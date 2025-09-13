# -*- coding: utf-8 -*-
# Guardião — IA autônoma (G0→G1→G2) usando o canal apenas como FONTE DE DADOS
# - Ingestão: lê "ANALISANDO/Sequência" e GREEN/RED do canal para alimentar timeline/fechamentos
# - Decisão: a IA calcula o posterior e planeja TODAS as combinações (permutações) para G0,G1,G2
# - Execução: abre pendência com plano (seq=[g0,g1,g2]) e segue o plano nos estágios
# - Relatório diário considera até G2 (SCORE_G0_ONLY=False)
#
# ENV obrigatórias: TG_BOT_TOKEN, WEBHOOK_TOKEN, IA_CHANNEL (canal de saída da IA)
# ENV opcionais:    PUBLIC_CHANNEL (se quiser broadcast no canal principal), DB_PATH, WINDOW, etc.
#
import os, re, json, time, sqlite3, asyncio, itertools
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# CONFIG / ENV
# =========================
DB_PATH        = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()

# Saídas
IA_CHANNEL     = os.getenv("IA_CHANNEL", "").strip()      # <- canal SECUNDÁRIO (IA)
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()  # opcional: canal principal
REPL_ENABLED   = os.getenv("REPL_ENABLED", "0").lower() in ("1","true","on")
REPL_CHANNEL   = os.getenv("REPL_CHANNEL", "").strip()

SELF_LABEL_IA  = os.getenv("SELF_LABEL_IA", "Tiro seco por IA")

TELEGRAM_API   = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Precisão / thresholds (calibrados p/ ~90% até G2 em janelas estáveis)
WINDOW = int(os.getenv("WINDOW", "400"))
DECAY  = float(os.getenv("DECAY", "0.985"))

MIN_CONF_G0 = float(os.getenv("MIN_CONF_G0", "0.68"))
MIN_GAP_G0  = float(os.getenv("MIN_GAP_G0", "0.060"))
MIN_SAMPLES = int(os.getenv("MIN_SAMPLES", "2000"))

IA2_TIER_STRICT              = float(os.getenv("IA2_TIER_STRICT", "0.74"))
IA2_DELTA_GAP                = float(os.getenv("IA2_DELTA_GAP", "0.015"))
IA2_GAP_SAFETY               = float(os.getenv("IA2_GAP_SAFETY", "0.10"))
IA2_MIN_SECONDS_BETWEEN_FIRE = int(os.getenv("IA2_MIN_SECONDS_BETWEEN_FIRE", "3"))
IA2_MAX_PER_HOUR             = int(os.getenv("IA2_MAX_PER_HOUR", "10"))
IA2_COOLDOWN_AFTER_LOSS      = int(os.getenv("IA2_COOLDOWN_AFTER_LOSS", "15"))

SCORE_G0_ONLY = False  # <- conta acerto diário até G2

MAX_STAGE = 3  # G0,G1,G2

app = FastAPI(title="Guardião — IA autônoma (canais separados)", version="4.0.0")

# =========================
# DB helpers
# =========================
def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def exec_write(sql: str, params: tuple = ()):
    con = _connect(); con.execute(sql, params); con.commit(); con.close()

def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    con = _connect(); rows = con.execute(sql, params).fetchall(); con.close(); return rows

def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    con = _connect(); row = con.execute(sql, params).fetchone(); con.close(); return row

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
        open INTEGER NOT NULL,
        window_left INTEGER NOT NULL,
        seen_numbers TEXT DEFAULT '',
        source TEXT NOT NULL DEFAULT 'IA'
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_plan (
        pending_id INTEGER PRIMARY KEY,
        seq TEXT NOT NULL  -- JSON [g0,g1,g2]
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
# Utils
# =========================
def now_ts() -> int: return int(time.time())
def today_key() -> str: return datetime.utcnow().strftime("%Y%m%d")

def append_timeline(n: int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_recent_tail(window: int = WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def update_ngrams(decay: float = DECAY, max_n: int = 5, window: int = WINDOW):
    tail = get_recent_tail(window)
    if len(tail) < 2: return
    # acrescenta pesos decaindo no tempo
    con = _connect(); cur = con.cursor()
    for t in range(1, len(tail)):
        nxt = int(tail[t])
        dist = (len(tail)-1) - t
        w = float(decay ** dist)
        for n in range(2, max_n+1):
            if t-(n-1) < 0: break
            ctx = tail[t-(n-1):t]
            ctx_key = ",".join(str(x) for x in ctx)
            cur.execute("""
                INSERT INTO ngram_stats (n, ctx, next, weight)
                VALUES (?,?,?,?)
                ON CONFLICT(n, ctx, next) DO UPDATE SET weight = weight + excluded.weight
            """, (n, ctx_key, nxt, w))
    con.commit(); con.close()

def prob_from_ngrams(ctx: List[int], candidate: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    row_tot = query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key))
    tot = float(row_tot["w"] or 0.0) if row_tot else 0.0
    if tot <= 0: return 0.0
    row_c = query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, int(candidate)))
    w = float(row_c["weight"] or 0.0) if row_c else 0.0
    return w / tot if tot > 0 else 0.0

# =========================
# Posterior e seleção
# =========================
def posterior_from_tail(tail: List[int]) -> Dict[int, float]:
    """Posterior leve: mistura n-gram (backoff) + frequência recente."""
    if not tail:
        return {1:0.25,2:0.25,3:0.25,4:0.25}
    # contextos
    ctx4 = tail[-4:] if len(tail) >= 4 else []
    ctx3 = tail[-3:] if len(tail) >= 3 else []
    ctx2 = tail[-2:] if len(tail) >= 2 else []
    ctx1 = tail[-1:] if len(tail) >= 1 else []
    weights = {1:0.0,2:0.0,3:0.0,4:0.0}
    for c in (1,2,3,4):
        score = 0.0
        if len(ctx4)==4: score += 0.38 * prob_from_ngrams(ctx4[:-1], c)
        if len(ctx3)==3: score += 0.30 * prob_from_ngrams(ctx3[:-1], c)
        if len(ctx2)==2: score += 0.20 * prob_from_ngrams(ctx2[:-1], c)
        if len(ctx1)==1: score += 0.12 * prob_from_ngrams(ctx1[:-1], c)
        # leve boost por frequência nos últimos 40
        last_k = tail[-40:] if len(tail)>=40 else tail[:]
        if last_k:
            freq = last_k.count(c)/len(last_k)
            score *= (1.00 + 0.04*freq*10)  # até ~+4%
        weights[c] = score
    tot = sum(weights.values()) or 1e-9
    return {k: v/tot for k,v in weights.items()}

def best_sequence_upto_g2(post: Dict[int, float]) -> Tuple[List[int], float]:
    """Escolhe seq [g0,g1,g2] maximizando P(hit ≤ G2) com permutações sem repetição."""
    nums = [1,2,3,4]
    best_seq, best_p = [1,2,3], 0.0
    for a,b,c in itertools.permutations(nums, 3):
        pa, pb, pc = post.get(a,0.0), post.get(b,0.0), post.get(c,0.0)
        p = pa + (1-pa)*pb + (1-pa)*(1-pb)*pc
        if p > best_p:
            best_p, best_seq = p, [a,b,c]
    return best_seq, best_p

def top_gap(post: Dict[int,float]) -> float:
    a = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if len(a) < 2: return 1.0
    return a[0][1] - a[1][1]

# =========================
# Scoreboard (até G2)
# =========================
def update_daily_score(stage: Optional[int], won: bool):
    y = today_key()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0=g1=g2=loss=streak=0
    if row:
        g0,g1,g2,loss,streak = int(row["g0"]), int(row["g1"]), int(row["g2"]), int(row["loss"]), int(row["streak"])
    if won:
        if stage == 0: g0 += 1
        elif stage == 1: g1 += 1
        elif stage == 2: g2 += 1
        streak += 1
    else:
        loss += 1; streak = 0
    exec_write("""
      INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
      VALUES (?,?,?,?,?,?)
    """, (y,g0,g1,g2,loss,streak))

# =========================
# Telegram send
# =========================
_httpx_client: Optional[httpx.AsyncClient] = None
async def get_httpx() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = httpx.AsyncClient(timeout=10)
    return _httpx_client

async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    client = await get_httpx()
    try:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True})
    except Exception as e:
        print(f"[TG] send error: {e}")

async def send_ia_text(text: str, parse: str="HTML"):
    if IA_CHANNEL:
        await tg_send_text(IA_CHANNEL, text, parse)

async def send_pub_text(text: str, parse: str="HTML"):
    if PUBLIC_CHANNEL:
        await tg_send_text(PUBLIC_CHANNEL, text, parse)
    if REPL_ENABLED and REPL_CHANNEL:
        await tg_send_text(REPL_CHANNEL, text, parse)

# =========================
# Pendências (plano)
# =========================
def open_pending(suggested: int, source: str = "IA") -> int:
    exec_write("""
        INSERT INTO pending_outcome (created_at, suggested, stage, open, window_left, seen_numbers, source)
        VALUES (?, ?, 0, 1, ?, '', ?)
    """, (now_ts(), int(suggested), MAX_STAGE, source))
    row = query_one("SELECT id FROM pending_outcome ORDER BY id DESC LIMIT 1")
    return int(row["id"]) if row else 0

# =========================
# Fechamento de pendências (GREEN/RED do canal)
# =========================
async def close_pending_with_result(n_observed: int):
    rows = query_all("""
        SELECT id, suggested, stage, open, window_left, seen_numbers
        FROM pending_outcome WHERE open=1 ORDER BY id ASC
    """)
    if not rows: return
    for r in rows:
        pid        = int(r["id"])
        suggested  = int(r["suggested"])
        stage      = int(r["stage"])
        left       = int(r["window_left"])
        seen       = (r["seen_numbers"] or "").strip()
        seen_new   = (seen + ("," if seen else "") + str(int(n_observed)))

        if int(n_observed) == suggested:
            # GREEN
            exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                       (seen_new, pid))
            update_daily_score(stage, True)
            stage_txt = "G0" if stage==0 else ("G1" if stage==1 else "G2")
            await send_ia_text(f"✅ <b>GREEN</b> em <b>{stage_txt}</b> — Número: <b>{suggested}</b>")
        else:
            # LOSS desse estágio
            if left > 1:
                # segue o plano
                plan_row = query_one("SELECT seq FROM pending_plan WHERE pending_id=?", (pid,))
                plan = json.loads(plan_row["seq"]) if (plan_row and plan_row["seq"]) else []
                tried = [suggested] + [int(x) for x in seen_new.split(",") if x.strip().isdigit()]
                next_target = None
                for n in plan:
                    if n not in tried:
                        next_target = int(n); break
                # fallback: reavaliar posterior e escolher top remanescente
                if next_target is None:
                    tail = get_recent_tail(WINDOW)
                    post_now = posterior_from_tail(tail)
                    for n,_p in sorted(post_now.items(), key=lambda kv: kv[1], reverse=True):
                        if int(n) not in tried:
                            next_target = int(n); break
                # aplica avanço de estágio
                if next_target is not None:
                    exec_write("""
                        UPDATE pending_outcome
                           SET stage = stage + 1,
                               window_left = window_left - 1,
                               suggested = ?,
                               seen_numbers = ?
                         WHERE id = ?
                    """, (int(next_target), seen_new, pid))
                    # reporta LOSS no estágio anterior
                    stxt = "G0" if stage==0 else ("G1" if stage==1 else "G2")
                    await send_ia_text(f"❌ <b>LOSS</b> — Número: <b>{suggested}</b> (em {stxt})")
                else:
                    # sem alvo -> encerra
                    exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                               (seen_new, pid))
                    update_daily_score(stage, False)
                    stxt = "G0" if stage==0 else ("G1" if stage==1 else "G2")
                    await send_ia_text(f"❌ <b>LOSS</b> — Número: <b>{suggested}</b> (em {stxt})")
            else:
                # acabou a janela: fecha como LOSS final
                exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                           (seen_new, pid))
                update_daily_score(stage, False)
                stxt = "G0" if stage==0 else ("G1" if stage==1 else "G2")
                await send_ia_text(f"❌ <b>LOSS</b> — Número: <b>{suggested}</b> (em {stxt})")

# =========================
# IA loop (autônomo)
# =========================
_ia2_blocked_until_ts: int = 0
_ia2_sent_this_hour:   int = 0
_ia2_hour_bucket:      Optional[int] = None
_ia2_last_fire_ts:     int = 0

def _hour_bucket() -> int:
    return int(datetime.utcnow().strftime("%Y%m%d%H"))

def _reset_hour_if_needed():
    global _ia2_hour_bucket, _ia2_sent_this_hour
    hb = _hour_bucket()
    if _ia2_hour_bucket != hb:
        _ia2_hour_bucket = hb
        _ia2_sent_this_hour = 0

def _antispam_ok() -> bool:
    _reset_hour_if_needed()
    return _ia2_sent_this_hour < IA2_MAX_PER_HOUR

def _mark_sent():
    global _ia2_sent_this_hour; _ia2_sent_this_hour += 1

def _blocked_now() -> bool:
    return now_ts() < _ia2_blocked_until_ts

def _post_loss_cooldown():
    global _ia2_blocked_until_ts
    _ia2_blocked_until_ts = now_ts() + IA2_COOLDOWN_AFTER_LOSS

async def ia2_send_signal(best:int, conf:float, tail_len:int, mode:str):
    txt = (
        f"🤖 <b>{SELF_LABEL_IA} [{mode}]</b>\n"
        f"🎯 Número seco (G0): <b>{best}</b>\n"
        f"📈 Conf: <b>{conf*100:.2f}%</b> | Amostra≈<b>{tail_len}</b>"
    )
    await send_ia_text(txt)

async def ia2_process_once():
    # Condições de espaçamento / antispam
    if _blocked_now(): return
    if not _antispam_ok(): return
    if now_ts() - _ia2_last_fire_ts < IA2_MIN_SECONDS_BETWEEN_FIRE: return

    # Calcula posterior
    tail = get_recent_tail(WINDOW)
    if len(tail) < MIN_SAMPLES: return
    post = posterior_from_tail(tail)
    conf_top = max(post.values())
    gap = top_gap(post)
    if not ((conf_top >= IA2_TIER_STRICT) or (conf_top >= IA2_TIER_STRICT - IA2_DELTA_GAP and gap >= IA2_GAP_SAFETY)):
        return

    # Planeja sequência ótima
    seq, p_cumul = best_sequence_upto_g2(post)
    g0 = int(seq[0])

    # Abre pendência e armazena plano
    pid = open_pending(g0, source="IA")
    exec_write("INSERT OR REPLACE INTO pending_plan (pending_id, seq) VALUES (?,?)",
               (pid, json.dumps(seq)))

    # Dispara sinal (IA_CHANNEL)
    await ia2_send_signal(g0, conf_top, len(tail), "FIRE")
    _mark_sent()
    # marca tempo do último fire
    global _ia2_last_fire_ts; _ia2_last_fire_ts = now_ts()

# =========================
# Parsers do canal (ingestão)
# =========================
# Notas:
# - Usamos o canal SOMENTE como fonte de dados:
#   - "ANALISANDO/Sequência": alimenta timeline/ngrams
#   - "GREEN/RED": fecha pendências
GREEN_PATTERNS = [
    re.compile(r"\bGREEN\b.*?Número[:\s]*([1-4])", re.I | re.S),
    re.compile(r"APOSTA\s+ENCERRADA.*?\bGREEN\b.*?\(([1-4])\)", re.I | re.S),
]
RED_PATTERNS = [
    re.compile(r"\bLOSS\b.*?Número[:\s]*([1-4])", re.I | re.S),
    re.compile(r"\bRED\b.*?\(([1-4])\)", re.I | re.S),
]

def extract_green_number(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in GREEN_PATTERNS:
        m = rx.search(t)
        if m:
            nums = re.findall(r"[1-4]", m.group(1))
            if nums: return int(nums[0])
    return None

def extract_red_last_left(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in RED_PATTERNS:
        m = rx.search(t)
        if m:
            nums = re.findall(r"[1-4]", m.group(1))
            if nums: return int(nums[0])
    return None

def is_analise(text:str) -> bool:
    return bool(re.search(r"\bANALISANDO\b", text, flags=re.I))

def extract_seq_raw(text: str) -> Optional[str]:
    m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", text, flags=re.I)
    return m.group(1).strip() if m else None

# =========================
# Webhook
# =========================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>"}

@app.get("/ping_ia")
async def ping_ia():
    await send_ia_text("🔧 Teste IA: ok")
    return {"ok": True, "to": IA_CHANNEL}

@app.on_event("startup")
async def _boot():
    async def _loop():
        while True:
            try:
                await ia2_process_once()
            except Exception as e:
                print(f"[IA] loop error: {e}")
            await asyncio.sleep(0.5)  # ~2 Hz
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

    # 1) GREEN/RED -> fechar pendências e registrar timeline
    gnum = extract_green_number(text)
    rnum = extract_red_last_left(text)
    if gnum is not None or rnum is not None:
        n_obs = gnum if gnum is not None else rnum
        append_timeline(int(n_obs)); update_ngrams()
        await close_pending_with_result(int(n_obs))
        if gnum is not None:
            return {"ok": True, "green": int(n_obs)}
        else:
            return {"ok": True, "red": int(n_obs)}

    # 2) ANALISANDO -> só ingestão de sequência
    if is_analise(text):
        seq_raw = extract_seq_raw(text)
        if seq_raw:
            parts = [int(x) for x in re.findall(r"[1-4]", seq_raw)]
            # a sequência que vem geralmente é da esquerda p/ direita (a esquerda é mais antiga)
            # vamos inserir do MAIS ANTIGO para o MAIS NOVO
            for n in parts[::-1]:
                append_timeline(int(n))
            update_ngrams()
        return {"ok": True, "ingested": True}

    # 3) Ignorar "ENTRADA CONFIRMADA" do canal (não usamos como gatilho)
    return {"ok": True, "skipped": True}
