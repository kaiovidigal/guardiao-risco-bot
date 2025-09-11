# -*- coding: utf-8 -*-
# Fan Tan — Guardião (G0 + Recuperação G1/G2) — IA Cauda40 + Bigrama (anti‑flood)
#
# Destaques:
# - IA decide UM sinal por vez: abre outro só quando fechar (GREEN em G0/G1/G2 ou LOSS).
# - Dispara decisão apenas quando chega novo evento do canal (GREEN/RED/ANALISANDO).
# - Predição usa frequência na cauda (40) + bigrama (n=2) com backoff p/ unigram.
# - Mensagem explícita quando GREEN em recuperação: "✅ GREEN (recuperação G1/G2)".
# - Placar do dia atualizado e enviado IMEDIATAMENTE a cada fechamento.
# - Limites: intervalo mínimo entre sinais e máx/hora (anti‑flood adicional).
#
# Rotas:
#   GET  /                         -> ping
#   POST /webhook/<WEBHOOK_TOKEN>  -> recebe mensagens/edits do Telegram
#   GET  /debug/state              -> estado e métricas
#
# ENV obrigatórias:
#   TG_BOT_TOKEN, PUBLIC_CHANNEL (id ou @), WEBHOOK_TOKEN
# ENV opcionais:
#   DB_PATH (default /data/data.db), REPL_CHANNEL (espelho), MIN_SAMPLES (default 400)
#   IA_MIN_GAP (default 0.02), IA_MIN_SECONDS_BETWEEN (default 6), IA_MAX_PER_HOUR (default 25)

import os, re, json, time, sqlite3, asyncio
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime, timezone, timedelta
from collections import Counter

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# ---------------------
# Config / ENV
# ---------------------
DB_PATH       = os.getenv("DB_PATH", "/data/data.db")
TG_BOT_TOKEN  = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL= os.getenv("PUBLIC_CHANNEL", "").strip()  # canal réplica para enviar nossos sinais
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
REPL_CHANNEL  = os.getenv("REPL_CHANNEL", "").strip()    # opcional (espelho de debug)

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN e WEBHOOK_TOKEN.")

TELEGRAM_API  = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

WINDOW        = 400                 # janela da cauda para n-grams
CAUDA_K       = 40                  # cauda curta para frequência
DECAY         = 0.985               # decaimento para estatística
MIN_SAMPLES   = int(os.getenv("MIN_SAMPLES", "400"))   # liberar cedo
IA_MIN_GAP    = float(os.getenv("IA_MIN_GAP", "0.02"))
IA_MIN_SECONDS_BETWEEN = int(os.getenv("IA_MIN_SECONDS_BETWEEN", "6"))
IA_MAX_PER_HOUR = int(os.getenv("IA_MAX_PER_HOUR", "25"))

app = FastAPI(title="Guardião FanTan — IA Cauda40+Bigrama, anti-flood", version="1.0.0")

# ---------------------
# DB helpers
# ---------------------
def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def exec_write(sql: str, params: tuple = (), retries: int = 8, wait: float = 0.25):
    for _ in range(retries):
        try:
            con = _connect(); con.execute(sql, params); con.commit(); con.close(); return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(wait); continue
            raise
    raise sqlite3.OperationalError("DB locked após várias tentativas.")

def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    con = _connect(); rows = con.execute(sql, params).fetchall(); con.close(); return rows

def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    con = _connect(); row = con.execute(sql, params).fetchone(); con.close(); return row

def init_db():
    con = _connect(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram_stats(
        n INTEGER NOT NULL, ctx TEXT NOT NULL, next INTEGER NOT NULL, weight REAL NOT NULL,
        PRIMARY KEY(n,ctx,next)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_outcome(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
        suggested INTEGER NOT NULL, stage INTEGER NOT NULL, window_left INTEGER NOT NULL,
        open INTEGER NOT NULL DEFAULT 1, seen_numbers TEXT DEFAULT ''
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score(
        yyyymmdd TEXT PRIMARY KEY,
        g0 INTEGER NOT NULL DEFAULT 0,
        g1 INTEGER NOT NULL DEFAULT 0,
        g2 INTEGER NOT NULL DEFAULT 0,
        loss INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0
    )""")
    con.commit(); con.close()

init_db()

# ---------------------
# Utils
# ---------------------
def now_ts() -> int: return int(time.time())
def today_key() -> str: return datetime.now(timezone.utc).strftime("%Y%m%d")

async def tg_send(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True})

async def tg_broadcast(text: str):
    await tg_send(PUBLIC_CHANNEL, text, "HTML")
    if REPL_CHANNEL:
        await tg_send(REPL_CHANNEL, text, "HTML")

def append_timeline(n:int):
    exec_write("INSERT INTO timeline(created_at,number) VALUES(?,?)", (now_ts(), int(n)))

def get_tail(window:int=WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def update_ngrams(decay:float=DECAY, window:int=WINDOW):
    tail = get_tail(window)
    if len(tail) < 2: return
    for t in range(1, len(tail)):
        nxt = tail[t]; dist=(len(tail)-1)-t; w=(decay**dist)
        # unigram contexto último
        ctx1 = [tail[t-1]]
        ctx1_key = ",".join(map(str, ctx1))
        exec_write("""INSERT INTO ngram_stats(n,ctx,next,weight) VALUES(?,?,?,?)
                      ON CONFLICT(n,ctx,next) DO UPDATE SET weight=weight+excluded.weight""", (2, ctx1_key, int(nxt), float(w)))

def bigram_prob(prev:int, candidate:int) -> float:
    ctx_key = str(prev)
    row_tot = query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=2 AND ctx=?", (ctx_key,))
    tot = (row_tot["w"] or 0.0) if row_tot else 0.0
    if tot <= 0: return 0.0
    row = query_one("SELECT weight FROM ngram_stats WHERE n=2 AND ctx=? AND next=?", (ctx_key, candidate))
    w = (row["weight"] or 0.0) if row else 0.0
    return w / tot

def cauda40_freq_boost(tail:List[int]) -> Dict[int,float]:
    boosts={1:1.0,2:1.0,3:1.0,4:1.0}
    if not tail: return boosts
    k = tail[-CAUDA_K:] if len(tail)>=CAUDA_K else tail[:]
    c = Counter(k).most_common(2)
    if len(c)>=1: boosts[c[0][0]] = 1.05
    if len(c)>=2: boosts[c[1][0]] = 1.02
    return boosts

def choose_by_bigram(tail:List[int]) -> Tuple[int, float, Dict[int,float]]:
    if not tail:
        return 1, 0.25, {1:0.25,2:0.25,3:0.25,4:0.25}
    prev = tail[-1]
    raw = {x: bigram_prob(prev, x) for x in [1,2,3,4]}
    # backoff simples para uniforme caso tudo zero
    if sum(raw.values()) == 0.0:
        raw = {1:0.25,2:0.25,3:0.25,4:0.25}
    # aplica boost cauda40
    boost = cauda40_freq_boost(tail)
    for k in raw: raw[k] = raw[k] * boost.get(k,1.0)
    s = sum(raw.values()) or 1e-9
    post = {k: v/s for k,v in raw.items()}
    top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    gap = top[0][1] - top[1][1] if len(top) >= 2 else 1.0
    return top[0][0], gap, post

# ---------------------
# Parsers do canal (GREEN/RED/ANALISANDO)
# ---------------------
GREEN_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bGREEN\b.*?\((.*?)\)", re.I|re.S),
    re.compile(r"\bGREEN\b.*?\((.*?)\)", re.I|re.S),
    re.compile(r"\bGREEN\b.*?Número[:\s]*([1-4])", re.I|re.S),
]
RED_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\((.*?)\)", re.I|re.S),
    re.compile(r"\bRED\b.*?\((.*?)\)", re.I|re.S),
    re.compile(r"\bLOSS\b.*?Número[:\s]*([1-4])", re.I|re.S),
]
def _last_num_in_group(g:str) -> Optional[int]:
    nums = re.findall(r"[1-4]", g or "")
    return int(nums[-1]) if nums else None

def extract_green(text:str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in GREEN_PATTERNS:
        m = rx.search(t)
        if m:
            g = m.group(1) if m.lastindex else None
            return _last_num_in_group(g) if g is not None else _last_num_in_group(m.group(0))
    return None

def extract_red(text:str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in RED_PATTERNS:
        m = rx.search(t)
        if m:
            g = m.group(1) if m.lastindex else None
            return _last_num_in_group(g) if g is not None else _last_num_in_group(m.group(0))
    return None

def is_analise(text:str) -> bool:
    return bool(re.search(r"\bANALISANDO\b", text, flags=re.I))

def extract_seq_nums(text:str) -> List[int]:
    m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", text, flags=re.I)
    if not m: return []
    return [int(x) for x in re.findall(r"[1-4]", m.group(1))]

# ---------------------
# Pendência (1 por vez) & placar
# ---------------------
def has_open_pending() -> bool:
    row = query_one("SELECT COUNT(*) AS c FROM pending_outcome WHERE open=1", ())
    return (row["c"] or 0) > 0

def open_pending(number:int):
    exec_write("""INSERT INTO pending_outcome(created_at,suggested,stage,window_left,open,seen_numbers)
                  VALUES(?,?,?,?,1,'')""", (now_ts(), int(number), 0, 3))

def _close_all():
    exec_write("UPDATE pending_outcome SET open=0, window_left=0 WHERE open=1", ())

def observe_and_advance(n_observed:int) -> Tuple[bool, Optional[int]]:
    """
    Retorna (fechou, stage_win or None). Se ganhar em G1, retorna 1; em G2 retorna 2; em G0 retorna 0.
    """
    rows = query_all("""SELECT id, suggested, stage, window_left, seen_numbers
                        FROM pending_outcome WHERE open=1 ORDER BY id ASC""")
    if not rows: return False, None
    r = rows[0]
    pid, sug, stage, left, seen = r["id"], int(r["suggested"]), int(r["stage"]), int(r["window_left"]), (r["seen_numbers"] or "")
    seen_new = (seen + ("," if seen else "") + str(int(n_observed))).strip()
    if n_observed == sug:
        # Ganhou
        exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                   (seen_new, pid))
        return True, stage
    else:
        # Não bateu
        if left > 1:
            exec_write("UPDATE pending_outcome SET stage=stage+1, window_left=window_left-1, seen_numbers=? WHERE id=?",
                       (seen_new, pid))
            return False, None
        else:
            exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                       (seen_new, pid))
            return True, None  # fechou como LOSS

def bump_score_on_close(stage_win: Optional[int]):
    y = today_key()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0=g1=g2=loss=streak=0
    if row: g0,g1,g2,loss,streak = row["g0"],row["g1"],row["g2"],row["loss"],row["streak"]
    if stage_win is None:
        loss += 1; streak = 0
    elif stage_win == 0:
        g0 += 1; streak += 1
    elif stage_win == 1:
        g1 += 1; streak += 1
    elif stage_win == 2:
        g2 += 1; streak += 1
    exec_write("""INSERT INTO daily_score(yyyymmdd,g0,g1,g2,loss,streak) VALUES(?,?,?,?,?,?)
                  ON CONFLICT(yyyymmdd) DO UPDATE SET
                  g0=excluded.g0,g1=excluded.g1,g2=excluded.g2,loss=excluded.loss,streak=excluded.streak""",
               (y,g0,g1,g2,loss,streak))
    total = g0+g1+g2+loss
    acc = (g0+g1+g2)/total*100.0 if total else 0.0
    return g0,g1,g2,loss,streak,acc

async def send_close_message(stage_win: Optional[int], suggested:int):
    if stage_win is None:
        txt = f"❌ <b>LOSS</b> — Número: <b>{suggested}</b>"
    elif stage_win == 0:
        txt = f"✅ <b>GREEN</b> — Número: <b>{suggested}</b> (G0)"
    elif stage_win == 1:
        txt = f"✅ <b>GREEN (recuperação G1)</b> — Número: <b>{suggested}</b>"
    else:
        txt = f"✅ <b>GREEN (recuperação G2)</b> — Número: <b>{suggested}</b>"
    g0,g1,g2,loss,streak,acc = bump_score_on_close(stage_win)
    placar = (f"📊 <b>Placar (dia)</b>\n"
              f"🟢 G0:{g0} • G1:{g1} • G2:{g2} • 🔴 Loss:{loss}\n"
              f"✅ Acerto: {acc:.2f}% • 🔥 Streak: {streak}")
    await tg_broadcast(txt + "\n" + placar)

# ---------------------
# IA gate (anti-flood por hora e espaçamento)
# ---------------------
_last_fire_ts = 0
_hour_bucket  = None
_sents_this_hour = 0

def _hour_key() -> int:
    return int(datetime.now(timezone.utc).strftime("%Y%m%d%H"))

def can_fire_now() -> bool:
    global _hour_bucket, _sents_this_hour, _last_fire_ts
    hb = _hour_key()
    if _hour_bucket != hb:
        _hour_bucket = hb; _sents_this_hour = 0
    if _sents_this_hour >= IA_MAX_PER_HOUR:
        return False
    if now_ts() - (_last_fire_ts or 0) < IA_MIN_SECONDS_BETWEEN:
        return False
    return True

def mark_fire():
    global _sents_this_hour, _last_fire_ts
    _sents_this_hour += 1
    _last_fire_ts = now_ts()

# ---------------------
# Telegram Update model
# ---------------------
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

# ---------------------
# Web routes
# ---------------------
@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>"}

@app.get("/debug/state")
async def debug_state():
    tail = get_tail(WINDOW)
    y = today_key()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    ds = {"g0":0,"g1":0,"g2":0,"loss":0,"streak":0}
    if row: ds = {"g0":row["g0"],"g1":row["g1"],"g2":row["g2"],"loss":row["loss"],"streak":row["streak"]}
    return {
        "open_pending": has_open_pending(),
        "tail_len": len(tail),
        "min_samples": MIN_SAMPLES,
        "last_fire_age_s": max(0, now_ts()-(_last_fire_ts or 0)),
        "this_hour_sent": _sents_this_hour,
        "score_today": ds,
    }

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg:
        return {"ok": True}

    text = (msg.get("text") or msg.get("caption") or "").strip()

    # 1) Se vier GREEN/RED -> registra o ULTIMO numero no parênteses e fecha/avança pendência
    g = extract_green(text)
    r = extract_red(text)
    if g is not None or r is not None:
        n = g if g is not None else r
        append_timeline(n); update_ngrams()
        # fecha/avança
        closed, stage_win = observe_and_advance(n)
        if closed:
            # enviar msg de fechamento + placar
            last_row = query_one("SELECT suggested FROM pending_outcome ORDER BY id DESC LIMIT 1", ())
            suggested = int(last_row["suggested"]) if last_row else n
            await send_close_message(stage_win, suggested)
        # tentar abrir NOVO (um por vez) somente se não tiver aberto
        if not has_open_pending() and can_fire_now():
            tail = get_tail(WINDOW)
            if len(tail) >= MIN_SAMPLES:
                best, gap, _ = choose_by_bigram(tail)
                if gap >= IA_MIN_GAP:
                    open_pending(best)
                    mark_fire()
                    await tg_broadcast(f"🤖 <b>IA Cauda 40</b>\n🎯 Número seco (G0): <b>{best}</b>")
        return {"ok": True, "observed": n}

    # 2) ANALISANDO -> só registra a sequência (mantém 1 por vez)
    if is_analise(text):
        nums = extract_seq_nums(text)
        if nums:
            # Eles chegam como "a | b | c" (esquerda->direita). Queremos registrar do mais antigo p/ mais novo.
            for x in nums[::-1]:
                append_timeline(x)
            update_ngrams()
            # Após registrar, se NÃO há pendência aberta e pode disparar, decide um novo
            if not has_open_pending() and can_fire_now():
                tail = get_tail(WINDOW)
                if len(tail) >= MIN_SAMPLES:
                    best, gap, _ = choose_by_bigram(tail)
                    if gap >= IA_MIN_GAP:
                        open_pending(best)
                        mark_fire()
                        await tg_broadcast(f"🤖 <b>IA Cauda 40</b>\n🎯 Número seco (G0): <b>{best}</b>")
        return {"ok": True, "analise": True}

    # 3) Demais mensagens -> ignorar
    return {"ok": True, "skipped": True}
