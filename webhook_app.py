# -*- coding: utf-8 -*-
# Guardião AUTO — IA independente (G0/G1/G2) com metas de precisão (>=80% para FIRE)
# - Lê apenas os números reais (mensagens "ANALISANDO" com Sequência: ...).
# - Gera sinais de forma autônoma; NÃO depende do "Número seco (G0)" do canal.
# - Publica TUDO (sinais + GREEN/LOSS) apenas no IA_CHANNEL.
# - Recuperação até G2 (janela de 3 observações por pendência).
#
# Reqs (Render): fastapi, httpx, pydantic
#
# ENV esperadas:
#   TG_BOT_TOKEN      -> token do bot do Telegram
#   WEBHOOK_TOKEN     -> segredo da rota /webhook/<token>
#   IA_CHANNEL        -> chat_id do canal de sinais (ex.: -1003052132833)
#   DB_PATH           -> opcional (padrão: /data/data.db)
#   WINDOW            -> tamanho da cauda p/ n-gram (padrão: 400)
#   DECAY             -> decaimento de peso das amostras (padrão: 0.985)
#   MIN_CONF_G0       -> confiança mínima p/ FIRE (padrão: 0.80)
#   MIN_GAP_G0        -> gap mínimo entre top1 e top2 (padrão: 0.05)
#   MIN_SAMPLES       -> amostras mínimas p/ FIRE (padrão: 2000)
#   IA_MAX_PER_HOUR   -> anti-spam por hora (padrão: 30)
#   IA_COOLDOWN_SEC   -> descanso após LOSS em G0 (padrão: 8s)
#   IA_MIN_BETWEEN    -> intervalo mínimo entre sinais (padrão: 3s)
#
import os, re, json, time, sqlite3, asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# Config
# =========================
DB_PATH         = os.getenv("DB_PATH", "/data/data.db")
TG_BOT_TOKEN    = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN   = os.getenv("WEBHOOK_TOKEN", "").strip()
IA_CHANNEL      = os.getenv("IA_CHANNEL", "-1003052132833").strip()

WINDOW          = int(os.getenv("WINDOW", "400"))
DECAY           = float(os.getenv("DECAY", "0.985"))

# Pesos do backoff 4-3-2-1
W4, W3, W2, W1  = 0.40, 0.30, 0.20, 0.10

MIN_CONF_G0     = float(os.getenv("MIN_CONF_G0", "0.80"))
MIN_GAP_G0      = float(os.getenv("MIN_GAP_G0",  "0.05"))
MIN_SAMPLES     = int(os.getenv("MIN_SAMPLES",   "2000"))

IA_MAX_PER_HOUR = int(os.getenv("IA_MAX_PER_HOUR", "30"))
IA_COOLDOWN_SEC = int(os.getenv("IA_COOLDOWN_SEC", "8"))
IA_MIN_BETWEEN  = int(os.getenv("IA_MIN_BETWEEN",  "3"))

TELEGRAM_API    = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI(title="Guardião AUTO — IA independente", version="1.0.0")

# =========================
# DB
# =========================
def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def exec_write(sql: str, params: tuple = (), retries: int = 8, wait: float = 0.2):
    for _ in range(retries):
        try:
            con = _connect()
            con.execute(sql, params)
            con.commit()
            con.close()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(wait)
                continue
            raise
    raise sqlite3.OperationalError("DB bloqueado (várias tentativas).")

def query_all(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    con = _connect(); rows = con.execute(sql, params).fetchall(); con.close(); return rows

def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    con = _connect(); row  = con.execute(sql, params).fetchone();  con.close(); return row

def init_db():
    con = _connect(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram_stats (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, next INTEGER NOT NULL, weight REAL NOT NULL,
        PRIMARY KEY (n, ctx, next)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_outcome (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        suggested INTEGER NOT NULL,
        stage INTEGER NOT NULL,
        window_left INTEGER NOT NULL,
        open INTEGER NOT NULL,
        seen TEXT NOT NULL DEFAULT ''
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY,
        g0 INTEGER NOT NULL DEFAULT 0,
        g1 INTEGER NOT NULL DEFAULT 0,
        g2 INTEGER NOT NULL DEFAULT 0,
        loss INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0
    )""")
    con.commit(); con.close()
init_db()

# =========================
# Telegram
# =========================
_httpx_client: Optional[httpx.AsyncClient] = None
async def _http() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = httpx.AsyncClient(timeout=10)
    return _httpx_client

@app.on_event("shutdown")
async def _shutdown():
    global _httpx_client
    try:
        if _httpx_client: await _httpx_client.aclose()
    except Exception:
        pass

async def tg_send(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    try:
        client = await _http()
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse,
                                "disable_web_page_preview": True})
    except Exception as e:
        print(f"[TG] send error: {e}")

async def send_signal(best:int, conf:float, samples:int):
    txt = (
        f"🤖 <b>Tiro seco por IA [FIRE]</b>\n"
        f"🎯 Número seco (G0): <b>{best}</b>\n"
        f"📈 Conf: <b>{conf*100:.2f}%</b> | Amostra≈<b>{samples}</b>"
    )
    await tg_send(IA_CHANNEL, txt)

async def send_green(n:int, stage_txt:str="G0"):
    await tg_send(IA_CHANNEL, f"✅ <b>GREEN</b> em <b>{stage_txt}</b> — Número: <b>{n}</b>")

async def send_loss(n:int, stage_txt:str="G0"):
    await tg_send(IA_CHANNEL, f"❌ <b>LOSS</b> — Número: <b>{n}</b> (em {stage_txt})")

# =========================
# Util
# =========================
def now_ts() -> int: return int(time.time())
def today_key() -> str: return datetime.now(timezone.utc).strftime("%Y%m%d")

def update_daily(stage: int, won: bool):
    y = today_key()
    r = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    if not r: g0=g1=g2=loss=streak=0
    else:     g0,g1,g2,loss,streak = r["g0"],r["g1"],r["g2"],r["loss"],r["streak"]
    if won:
        (g0,g1,g2)[stage if 0<=stage<=2 else 0]
        if   stage==0: g0+=1
        elif stage==1: g1+=1
        elif stage==2: g2+=1
        streak+=1
    else:
        loss+=1; streak=0
    exec_write("""INSERT INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
                  VALUES (?,?,?,?,?,?)
                  ON CONFLICT(yyyymmdd) DO UPDATE SET
                  g0=excluded.g0, g1=excluded.g1, g2=excluded.g2,
                  loss=excluded.loss, streak=excluded.streak
               """, (y,g0,g1,g2,loss,streak))

def append_timeline(n: int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def recent_tail(k:int=WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (k,))
    return [r["number"] for r in rows][::-1]

def update_ngrams(decay: float = DECAY, max_n:int=4, window:int=WINDOW):
    tail = recent_tail(window)
    if len(tail) < 2: return
    for t in range(1, len(tail)):
        nxt = tail[t]; dist = (len(tail)-1)-t; w = (decay ** dist)
        for n in range(2, max_n+1):
            if t-(n-1) < 0: break
            ctx = tail[t-(n-1):t]
            ctx_key = ",".join(str(x) for x in ctx)
            exec_write("""INSERT INTO ngram_stats (n,ctx,next,weight)
                          VALUES (?,?,?,?)
                          ON CONFLICT(n,ctx,next) DO UPDATE SET
                          weight = weight + excluded.weight
                       """, (n, ctx_key, int(nxt), float(w)))

def prob_from_ng(ctx: List[int], candidate: int) -> float:
    n = len(ctx)+1
    if n < 2 or n > 4: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    tot = query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key))
    totw = float(tot["w"] or 0.0) if tot else 0.0
    if totw <= 0: return 0.0
    row = query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, int(candidate)))
    w = float(row["weight"] or 0.0) if row else 0.0
    return w / totw

# =========================
# Predição
# =========================
def backoff_score(tail: List[int], candidate:int) -> float:
    if not tail: return 0.0
    ctx4 = tail[-4:] if len(tail)>=4 else []
    ctx3 = tail[-3:] if len(tail)>=3 else []
    ctx2 = tail[-2:] if len(tail)>=2 else []
    ctx1 = tail[-1:] if len(tail)>=1 else []
    parts = []
    if len(ctx4)==4: parts.append((W4, prob_from_ng(ctx4[:-1], candidate)))
    if len(ctx3)==3: parts.append((W3, prob_from_ng(ctx3[:-1], candidate)))
    if len(ctx2)==2: parts.append((W2, prob_from_ng(ctx2[:-1], candidate)))
    if len(ctx1)==1: parts.append((W1, prob_from_ng(ctx1[:-1], candidate)))
    return sum(w*p for w,p in parts)

def suggest_number() -> Tuple[Optional[int], float, int, Dict[int,float]]:
    base = [1,2,3,4]
    tail = recent_tail(WINDOW)
    if len(tail) < 5:
        return None, 0.0, len(tail), {k: 0.25 for k in base}
    scores: Dict[int,float] = {}
    for c in base:
        s = backoff_score(tail, c) or 1e-9
        scores[c] = s
    total = sum(scores.values()) or 1e-9
    post = {k: v/total for k,v in scores.items()}
    top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    best = top[0][0]; conf = top[0][1]
    gap  = top[0][1] - (top[1][1] if len(top)>1 else 0.0)

    # amostras ~ soma de pesos
    roww = query_one("SELECT SUM(weight) AS s FROM ngram_stats", ())
    samples = int((roww["s"] or 0.0) if roww else 0.0)

    if conf < MIN_CONF_G0 or gap < MIN_GAP_G0 or samples < MIN_SAMPLES:
        return None, conf, samples, post
    return best, conf, samples, post

# =========================
# IA loop com anti-spam
# =========================
_ia_last_fire_ts: int = 0
_ia_blocked_until: int = 0
_ia_hour_bucket: Optional[str] = None
_ia_sent_this_hour: int = 0

def _hour_key() -> str: return datetime.utcnow().strftime("%Y%m%d%H")

def _antispam_ok() -> bool:
    global _ia_hour_bucket, _ia_sent_this_hour
    hk = _hour_key()
    if _ia_hour_bucket != hk:
        _ia_hour_bucket = hk
        _ia_sent_this_hour = 0
    return _ia_sent_this_hour < IA_MAX_PER_HOUR

def _mark_sent():
    global _ia_sent_this_hour, _ia_last_fire_ts
    _ia_sent_this_hour += 1
    _ia_last_fire_ts = now_ts()

def _blocked() -> bool:
    return now_ts() < _ia_blocked_until

def _cooldown_after_g0_loss():
    global _ia_blocked_until
    _ia_blocked_until = now_ts() + IA_COOLDOWN_SEC

async def ia_process_once():
    if _blocked(): return
    if not _antispam_ok(): return
    if now_ts() - _ia_last_fire_ts < IA_MIN_BETWEEN: return

    best, conf, samples, post = suggest_number()
    if best is None: return

    # abrir pendência (G0, janela=3 -> G0/G1/G2)
    exec_write("""INSERT INTO pending_outcome (created_at, suggested, stage, window_left, open, seen)
                  VALUES (?,?,?,?,1,'')""", (now_ts(), int(best), 0, 3))
    await send_signal(int(best), conf, samples)
    _mark_sent()

# =========================
# Fechamento de pendências
# =========================
async def close_with_observed(n_observed: int):
    rows = query_all("SELECT id, suggested, stage, window_left, open, seen FROM pending_outcome WHERE open=1 ORDER BY id ASC")
    if not rows: return
    for r in rows:
        pid = r["id"]; sug = int(r["suggested"]); stage = int(r["stage"]); left = int(r["window_left"])
        seen = (r["seen"] or "")
        seen2 = (seen + ("," if seen else "") + str(n_observed))

        if n_observed == sug:
            # GREEN
            exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen=? WHERE id=?", (seen2, pid))
            await send_green(sug, "G0" if stage==0 else f"G{stage}")
            update_daily(stage if stage<=2 else 0, True)
        else:
            # segue para próximo estágio ou encerra
            if left > 1:
                exec_write("UPDATE pending_outcome SET stage=stage+1, window_left=window_left-1, seen=? WHERE id=?", (seen2, pid))
                if stage == 0:
                    # LOSS em G0 (publica)
                    await send_loss(sug, "G0")
                    update_daily(0, False)
                    _cooldown_after_g0_loss()
            else:
                exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen=? WHERE id=?", (seen2, pid))

# =========================
# Parsers de mensagens do canal
# =========================
# Considera mensagens de análise que trazem "Sequência: ..."
SEQ_RX = re.compile(r"Sequ[eê]ncia:\s*([^\n\r]+)", re.I)

def parse_sequence_numbers(text: str) -> List[int]:
    m = SEQ_RX.search(text)
    if not m: return []
    parts = re.findall(r"[1-4]", m.group(1))
    nums = [int(x) for x in parts]
    # timeline: garantir ordem cronológica (antigo -> novo)
    return nums[::-1]  # no seu canal, o último da string é o mais recente

def is_analise(text: str) -> bool:
    return bool(re.search(r"\bANALISANDO\b", text, flags=re.I)) or ("Sequência:" in text)

# =========================
# FastAPI models & routes
# =========================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

@app.get("/")
async def root():
    return {"ok": True, "detail": "Guardião AUTO — use POST /webhook/<WEBHOOK_TOKEN> e /ping_ia"}

@app.get("/ping_ia")
async def ping_ia():
    try:
        await tg_send(IA_CHANNEL, "🔧 Teste IA: ok")
        return {"ok": True, "sent": True, "to": IA_CHANNEL}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.on_event("startup")
async def _boot():
    async def _loop():
        while True:
            try:
                await ia_process_once()
            except Exception as e:
                print(f"[IA] loop error: {e}")
            await asyncio.sleep(0.5)
    asyncio.create_task(_loop())

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg: return {"ok": True}

    text = (msg.get("text") or msg.get("caption") or "").strip()

    # 1) ingestão da timeline
    if is_analise(text):
        nums = parse_sequence_numbers(text)
        if nums:
            # acrescenta apenas os novos, na ordem certa
            # (como defesa simples, só apenda todos — duplicatas não atrapalham n-gram laplace/decay)
            for n in nums:
                append_timeline(int(n))
            update_ngrams()
        return {"ok": True, "analise": True, "added": len(nums)}

    # 2) fallback: se aparecer uma mensagem explícita com GREEN/LOSS contendo número, fecha janelas
    g = re.search(r"\bGREEN\b.*?([1-4])", text, flags=re.I)
    l = re.search(r"\bLOSS\b.*?([1-4])",  text, flags=re.I)
    if g or l:
        n = int((g or l).group(1))
        append_timeline(n); update_ngrams()
        await close_with_observed(n)
        return {"ok": True, "observed": n}

    return {"ok": True, "ignored": True}
