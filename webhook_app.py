# -*- coding: utf-8 -*-
# Guardião Fantan — IA Canal Secundário + AfterSync (postar só com resultado)
#
# O que esta versão faz:
# - Lê eventos do Telegram via /webhook/<WEBHOOK_TOKEN>
# - Alimenta timeline (números 1..4) quando vê GREEN/RED do canal de origem
# - Se houver "Entrar após o X", enfileira o pedido e SÓ dispara o tiro da IA
#   quando X realmente aparecer no timeline (sincronismo perfeito)
# - Ao disparar, calcula o "número seco" (n-gram leve) e publica no IA_CHANNEL
#   JÁ com o resultado GREEN/LOSS do round atual (não publica FIRE antecipado)
# - Roteia as mensagens da IA apenas para IA_CHANNEL (não interfere no canal público)
# - Endpoint de teste: /debug/ping_ia?key=<FLUSH_KEY>&text=...
#
# ENV obrigatórias: TG_BOT_TOKEN, WEBHOOK_TOKEN
# ENV recomendadas: IA_CHANNEL, FLUSH_KEY, PUBLIC_CHANNEL (opcional)
#
import os, re, json, time, sqlite3, asyncio
from typing import Optional, List, Tuple, Dict
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
DB_PATH = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
IA_CHANNEL     = os.getenv("IA_CHANNEL", "").strip()   # Canal secundário IA (ex: -1002796105884)
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()  # opcional (não usamos para IA)
FLUSH_KEY      = os.getenv("FLUSH_KEY", "meusegredo123").strip()
TELEGRAM_API   = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Hiperparâmetros simples
WINDOW = 400
DECAY  = 0.985
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12
GAP_MIN, MIN_CONF_G0, MIN_GAP_G0, MIN_SAMPLES = 0.08, 0.55, 0.04, 400  # amostra reduzida para iniciar mais cedo
SELF_LABEL_IA = os.getenv("SELF_LABEL_IA", "Tiro seco por IA")

app = FastAPI(title="Guardião — IA secundário + AfterSync", version="1.0.0")

# =========================
# DB helpers
# =========================
def _ensure_db_dir():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def _connect():
    _ensure_db_dir()
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def exec_write(sql: str, params: tuple = ()):
    con = _connect(); con.execute(sql, params); con.commit(); con.close()

def query_all(sql: str, params: tuple = ()):
    con = _connect(); rows = con.execute(sql, params).fetchall(); con.close(); return rows

def query_one(sql: str, params: tuple = ()):
    con = _connect(); row = con.execute(sql, params).fetchone(); con.close(); return row

def init_db():
    con = _connect(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, number INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram_stats (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, next INTEGER NOT NULL, weight REAL NOT NULL,
        PRIMARY KEY (n, ctx, next)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS wait_after (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        strategy TEXT,
        pattern_key TEXT NOT NULL,
        base TEXT NOT NULL,
        after_num INTEGER NOT NULL,
        ttl_seconds INTEGER NOT NULL DEFAULT 180
    )""")
    con.commit(); con.close()
init_db()

# =========================
# Utils
# =========================
def now_ts(): return int(time.time())

def append_timeline(n: int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_recent_tail(window: int = WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def update_ngrams(decay: float = DECAY, max_n: int = 5, window: int = WINDOW):
    tail = get_recent_tail(window)
    if len(tail) < 2: return
    for t in range(1, len(tail)):
        nxt = tail[t]; dist = (len(tail)-1) - t; w = (decay ** dist)
        for n in range(2, max_n+1):
            if t-(n-1) < 0: break
            ctx = tail[t-(n-1):t]; ctx_key = ",".join(str(x) for x in ctx)
            exec_write("""INSERT INTO ngram_stats (n, ctx, next, weight)
                          VALUES (?,?,?,?)
                          ON CONFLICT(n,ctx,next) DO UPDATE SET weight=weight+excluded.weight""",
                       (n, ctx_key, int(nxt), float(w)))

# Telegram
_httpx_client: Optional[httpx.AsyncClient] = None
async def get_httpx() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = httpx.AsyncClient(timeout=10)
    return _httpx_client

async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    client = await get_httpx()
    await client.post(f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True})

async def send_ia(text: str, parse: str="HTML"):
    if IA_CHANNEL: await tg_send_text(IA_CHANNEL, text, parse)

# =========================
# Parsing helpers (canal origem)
# =========================
MUST_HAVE = (r"ENTRADA\s+CONFIRMADA", r"Mesa:\s*Fantan\s*-\s*Evolution")
MUST_NOT  = (r"\bANALISANDO\b", r"\bPlacar do dia\b", r"\bAPOSTA ENCERRADA\b")

def is_real_entry(text: str) -> bool:
    t = re.sub(r"\s+", " ", text).strip()
    for bad in MUST_NOT:
        if re.search(bad, t, flags=re.I): return False
    for good in MUST_HAVE:
        if not re.search(good, t, flags=re.I): return False
    has_ctx = any(re.search(p, t, flags=re.I) for p in [
        r"Sequ[eê]ncia:\s*[\d\s\|\-]+",
        r"\bKWOK\s*[1-4]\s*-\s*[1-4]",
        r"\bSS?H\s*[1-4](?:-[1-4]){0,3}",
        r"\bODD\b|\bEVEN\b",
        r"Entrar\s+ap[oó]s\s+o\s+[1-4]"
    ])
    return bool(has_ctx)

def extract_seq_raw(text: str) -> Optional[str]:
    m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", text, flags=re.I)
    return m.group(1).strip() if m else None

def extract_after_num(text: str) -> Optional[int]:
    m = re.search(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", text, flags=re.I)
    return int(m.group(1)) if m else None

GREEN_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bGREEN\b.*?\((\d)\)", re.I | re.S),
    re.compile(r"\bGREEN\b.*?Número[:\s]*([1-4])", re.I | re.S),
    re.compile(r"\bGREEN\b.*?\(([1-4])\)", re.I | re.S),
]
RED_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\((.*?)\)", re.I | re.S),
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

# =========================
# N-gram model simples
# =========================
def prob_from_ngrams(ctx: List[int], candidate: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(map(str, ctx))
    row = query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key))
    tot = (row["w"] or 0.0) if row else 0.0
    if tot <= 0: return 0.0
    row2 = query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, candidate))
    w = (row2["weight"] or 0.0) if row2 else 0.0
    return w / tot

def suggest_number(base: List[int], after_num: Optional[int]) -> Tuple[Optional[int], float, int, Dict[int,float]]:
    if not base: base = [1,2,3,4]
    tail = get_recent_tail(WINDOW)
    if len(tail) < MIN_SAMPLES:
        return None, 0.0, len(tail), {k: 1/len(base) for k in base}

    # Se exigir "após X", só aceite quando X for o último
    if after_num is not None:
        if not tail or tail[-1] != int(after_num):
            return None, 0.0, len(tail), {k: 1/len(base) for k in base}

    def backoff(ctx: List[int], c: int) -> float:
        parts = []
        if len(ctx)>=4: parts.append((W4, prob_from_ngrams(ctx[-4:-1], c)))
        if len(ctx)>=3: parts.append((W3, prob_from_ngrams(ctx[-3:-1], c)))
        if len(ctx)>=2: parts.append((W2, prob_from_ngrams(ctx[-2:-1], c)))
        if len(ctx)>=1: parts.append((W1, prob_from_ngrams(ctx[-1:], c)))
        return sum(w*p for w,p in parts) or 1e-6

    scores: Dict[int, float] = {}
    for c in base:
        ng = backoff(tail, c)
        scores[c] = ng

    total = sum(scores.values()) or 1e-9
    post = {k: v/total for k,v in scores.items()}
    a = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if len(a) < 1: return None, 0.0, len(tail), post
    if len(a) >= 2 and (a[0][1]-a[1][1]) < MIN_GAP_G0:
        return None, 0.0, len(tail), post
    best = a[0][0]
    conf = a[0][1]
    if conf < MIN_CONF_G0:
        return None, conf, len(tail), post
    return best, conf, len(tail), post

# =========================
# Fila "após X"
# =========================
def enqueue_wait_after(pattern_key: str, base: List[int], after_num: int, ttl:int=180):
    exec_write("""INSERT INTO wait_after (created_at, strategy, pattern_key, base, after_num, ttl_seconds)
                  VALUES (?,?,?,?,?,?)""",
               (now_ts(), "", pattern_key, json.dumps(base), int(after_num), int(ttl)))

def consume_wait_after(n_observed: int) -> List[sqlite3.Row]:
    now = now_ts()
    rows = query_all("""SELECT id, created_at, strategy, pattern_key, base, after_num, ttl_seconds
                        FROM wait_after
                        WHERE after_num=? AND (? - created_at) <= ttl_seconds
                        ORDER BY id ASC""", (int(n_observed), now))
    exec_write("DELETE FROM wait_after WHERE after_num=?", (int(n_observed),))
    return rows

# =========================
# Mensagens IA (apenas quando sai resultado)
# =========================
async def ia_post_result(best:int, result_num:int, stage_txt:str, after_num: Optional[int], conf: float, samples: int):
    aft = f"\n       Após número {after_num}" if after_num is not None else ""
    status = "✅ <b>GREEN</b>" if int(best) == int(result_num) else "❌ <b>LOSS</b>"
    msg = (
        f"🤖 <b>{SELF_LABEL_IA}</b>\n"
        f"🎯 Número seco ({stage_txt}): <b>{best}</b>{aft}\n"
        f"📈 Conf: <b>{conf*100:.2f}%</b> | Amostra≈<b>{samples}</b>\n"
        f"{status} — Número: <b>{result_num}</b> (em {stage_txt})"
    )
    await send_ia(msg)

# =========================
# Webhook models
# =========================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

# =========================
# Routes
# =========================
@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>"}

@app.get("/debug/ping_ia")
async def debug_ping_ia(key: str = "", text: str = "ping"):
    if key != FLUSH_KEY:
        return {"ok": False, "error": "unauthorized"}
    await send_ia(f"🔧 Teste IA: {text}")
    return {"ok": True, "sent": True, "to": IA_CHANNEL}

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
    t = re.sub(r"\s+", " ", text)

    # 1) GREEN/RED — fecha round e dispara fila "após X" (sincronizado)
    gnum = extract_green_number(t)
    rnum = extract_red_last_left(t)
    if gnum is not None or rnum is not None:
        n_observed = gnum if gnum is not None else rnum
        append_timeline(int(n_observed))
        update_ngrams()

        # Processa todas as esperas que aguardavam 'após n_observed'
        waits = consume_wait_after(int(n_observed))
        results = []
        for w in waits:
            try:
                base = json.loads(w["base"]) if w["base"] else [1,2,3,4]
            except Exception:
                base = [1,2,3,4]
            after_num = int(n_observed)
            best, conf, samples, post = suggest_number(base, after_num)
            if best is None:
                results.append({"queued_after": after_num, "skipped": "low_conf_or_gap", "samples": samples})
                continue
            # Posta no IA_CHANNEL já com o resultado do round atual
            await ia_post_result(int(best), int(n_observed), "G0", after_num, conf, samples)
            results.append({"queued_after": after_num, "best": int(best), "result": int(n_observed)})
        return {"ok": True, "observed": int(n_observed), "processed_waits": results}

    # 2) ANALISANDO — alimenta timeline (aprendizado)
    if is_analise(t):
        seq_raw = extract_seq_raw(t)
        if seq_raw:
            parts = re.findall(r"[1-4]", seq_raw)
            seq = [int(x) for x in parts]
            for n in seq[::-1]:
                append_timeline(n)
            update_ngrams()
        return {"ok": True, "analise": True}

    # 3) ENTRADA CONFIRMADA — enfileira "após X" (não posta FIRE)
    if not is_real_entry(t):
        return {"ok": True, "skipped": True}

    after_num = extract_after_num(t)
    # Base/padrão simples a partir da sequência (se existir)
    base = [1,2,3,4]
    seq_raw = extract_seq_raw(t)
    if seq_raw:
        nums = [int(x) for x in re.findall(r"[1-4]", seq_raw)]
        seen, b = set(), []
        for n in nums:
            if n not in seen:
                seen.add(n); b.append(n)
            if len(b) == 3: break
        if b: base = b

    if after_num is not None:
        enqueue_wait_after("GEN", base, int(after_num), ttl=180)
        return {"ok": True, "queued_after": int(after_num), "base": base}

    # Se não tem "após X", não dispara FIRE (apenas aprendizado via ANALISANDO/GREEN/RED)
    return {"ok": True, "no_after": True}
