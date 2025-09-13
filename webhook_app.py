# -*- coding: utf-8 -*-
# Guardião — IA independente + um único canal de sinais/resultados
# - SIGNAL_CHANNEL recebe TUDO (sinais G0 gerados pela IA, GREEN/LOSS, progressão G1/G2)
# - IA ignora “Entrada confirmada” como gatilho (usa só timeline/estatísticas)
# - Fechamento de pendências com estágios (G0->G1->G2) e mensagens não silenciosas
# - Anti-spam, debouncing e thresholds de precisão
#
# Execução local:
#   uvicorn webhook_app:app --host 0.0.0.0 --port 8000
#
# Variáveis de ambiente essenciais:
#   TG_BOT_TOKEN      -> token do bot
#   WEBHOOK_TOKEN     -> token do endpoint /webhook/<token>
#   SIGNAL_CHANNEL    -> ID do canal onde TUDO será publicado (ex.: -1003052132833)
#
import os, re, json, time, sqlite3, asyncio, shutil
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone
from collections import Counter

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# ENV / CONFIG
# =========================
DB_PATH = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()

# Um ÚNICO canal para tudo (sinais + resultados)
SIGNAL_CHANNEL = os.getenv("SIGNAL_CHANNEL", "-1003052132833").strip()

# Desacoplar decisão da IA do canal de entrada
USE_CHANNEL_G0 = False  # False = ignora “Entrada confirmada” como gatilho de G0

# Caminho p/ DB antigo (migração) — opcional
OLD_DB_CANDIDATES = [
    "/var/data/data.db",
    "/opt/render/project/src/data.db",
    "/opt/render/project/src/data/data.db",
    "/data/data.db",
]

def _ensure_db_dir():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except Exception as e:
        print(f"[DB] Falha ao criar dir DB: {e}")

def _migrate_old_db_if_needed():
    if os.path.exists(DB_PATH):
        return
    for src in OLD_DB_CANDIDATES:
        if os.path.exists(src):
            try:
                _ensure_db_dir()
                shutil.copy2(src, DB_PATH)
                print(f"[DB] Migrado {src} -> {DB_PATH}")
                return
            except Exception as e:
                print(f"[DB] Erro migrando {src} -> {DB_PATH}: {e}")

_ensure_db_dir()
_migrate_old_db_if_needed()

TELEGRAM_API   = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ====== Hyperparams / IA ======
WINDOW = int(os.getenv("WINDOW", "400"))     # cauda analisada
DECAY  = float(os.getenv("DECAY", "0.985"))

# pesos do backoff n-gram
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12

# exponente de fusão (padrão/estratégia)
ALPHA, BETA, GAMMA = 1.05, 0.70, 0.40

# separação mínima de prob. entre 1º e 2º
GAP_MIN = float(os.getenv("GAP_MIN", "0.08"))

# Confiança mínima para soltar G0
MIN_CONF_G0 = float(os.getenv("MIN_CONF_G0", "0.62"))
# Amostra mínima de histórico (proxy de robustez)
MIN_SAMPLES = int(os.getenv("MIN_SAMPLES", "1000"))

# IA loop / segurança
IA2_TIER_STRICT             = float(os.getenv("IA2_TIER_STRICT", "0.62"))
IA2_DELTA_GAP               = float(os.getenv("IA2_DELTA_GAP", "0.03"))
IA2_GAP_SAFETY              = float(os.getenv("IA2_GAP_SAFETY", "0.08"))
IA2_MAX_PER_HOUR            = int(os.getenv("IA2_MAX_PER_HOUR", "30"))
IA2_COOLDOWN_AFTER_LOSS     = int(os.getenv("IA2_COOLDOWN_AFTER_LOSS", "8"))
IA2_MIN_SECONDS_BETWEEN_FIRE= int(os.getenv("IA2_MIN_SECONDS_BETWEEN_FIRE", "2"))

# Estágios de recuperação
MAX_STAGE     = 3  # G0,G1,G2
SCORE_G0_ONLY = True  # placar conta só G0

SELF_LABEL_IA = os.getenv("SELF_LABEL_IA", "Tiro seco por IA")
INTEL_DIR = os.getenv("INTEL_DIR", "/var/data/ai_intel").rstrip("/")
os.makedirs(os.path.join(INTEL_DIR, "snapshots"), exist_ok=True)

app = FastAPI(title="Guardião — IA independente", version="4.0.0")

# =========================
# SQLite
# =========================
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
    raise sqlite3.OperationalError("Banco bloqueado após várias tentativas.")

def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    con = _connect()
    rows = con.execute(sql, params).fetchall()
    con.close()
    return rows

def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    con = _connect()
    row = con.execute(sql, params).fetchone()
    con.close()
    return row

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
    cur.execute("""CREATE TABLE IF NOT EXISTS stats_pattern (
        pattern_key TEXT NOT NULL, number INTEGER NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (pattern_key, number)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS stats_strategy (
        strategy TEXT NOT NULL, number INTEGER NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (strategy, number)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS suggestions (
        source_msg_id INTEGER PRIMARY KEY, strategy TEXT, seq_raw TEXT,
        context_key TEXT, pattern_key TEXT, base TEXT,
        suggested_number INTEGER, stage TEXT, sent_at INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS last_by_strategy (
        strategy TEXT PRIMARY KEY, source_msg_id INTEGER, suggested_number INTEGER,
        context_key TEXT, pattern_key TEXT, stage TEXT, created_at INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY,
        g0 INTEGER NOT NULL DEFAULT 0,
        g1 INTEGER NOT NULL DEFAULT 0,
        g2 INTEGER NOT NULL DEFAULT 0,
        loss INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_outcome (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
        strategy TEXT, suggested INTEGER NOT NULL, stage INTEGER NOT NULL,
        open INTEGER NOT NULL, window_left INTEGER NOT NULL,
        seen_numbers TEXT DEFAULT '',
        announced INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'IA'
    )""")
    con.commit()
    # Migrations idempotentes
    for alter in [
        "ALTER TABLE pending_outcome ADD COLUMN seen_numbers TEXT DEFAULT ''",
        "ALTER TABLE pending_outcome ADD COLUMN announced INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE pending_outcome ADD COLUMN source TEXT NOT NULL DEFAULT 'IA'",
    ]:
        try: cur.execute(alter); con.commit()
        except sqlite3.OperationalError: pass
    con.close()

init_db()

# =========================
# Telegram helpers
# =========================
_httpx_client: Optional[httpx.AsyncClient] = None

async def get_httpx() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = httpx.AsyncClient(timeout=10)
    return _httpx_client

@app.on_event("shutdown")
async def _shutdown():
    global _httpx_client
    try:
        if _httpx_client:
            await _httpx_client.aclose()
    except Exception as e:
        print(f"[HTTPX] close error: {e}")

async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id:
        return
    try:
        client = await get_httpx()
        asyncio.create_task(
            client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse,
                    "disable_web_page_preview": True,
                },
            )
        )
    except Exception as e:
        print(f"[TG] send error: {e}")

async def tg_broadcast(text: str, parse: str="HTML"):
    # ÚNICO canal para tudo
    if SIGNAL_CHANNEL:
        await tg_send_text(SIGNAL_CHANNEL, text, parse)

# aliases (mantidos para clareza)
async def send_ia_text(text: str, parse: str="HTML"):      await tg_broadcast(text, parse)
async def send_green_channel(n:int, stage_txt:str="G0"):   await tg_broadcast(f"✅ <b>GREEN</b> em <b>{stage_txt}</b> — Número: <b>{n}</b>")
async def send_loss_channel(n:int, stage_txt:str="G0"):    await tg_broadcast(f"❌ <b>LOSS</b> — Número: <b>{n}</b> (em {stage_txt})")
async def send_green_ia(n:int, stage_txt:str="G0"):        await send_green_channel(n, stage_txt)
async def send_loss_ia(n:int, stage_txt:str="G0"):         await send_loss_channel(n, stage_txt)

# =========================
# Timeline & modelos
# =========================
def now_ts() -> int: return int(time.time())
def today_key_local() -> str: return datetime.now(timezone.utc).strftime("%Y%m%d")

def append_timeline(n: int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_recent_tail(window: int = WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def update_ngrams(decay: float = DECAY, max_n: int = 5, window: int = WINDOW):
    tail = get_recent_tail(window)
    if len(tail) < 2: return
    for t in range(1, len(tail)):
        nxt = tail[t]
        dist = (len(tail)-1) - t
        w = (decay ** dist)
        for n in range(2, max_n+1):
            if t-(n-1) < 0: break
            ctx = tail[t-(n-1):t]
            ctx_key = ",".join(str(x) for x in ctx)
            exec_write("""
                INSERT INTO ngram_stats (n, ctx, next, weight)
                VALUES (?,?,?,?)
                ON CONFLICT(n, ctx, next) DO UPDATE SET weight = weight + excluded.weight
            """, (n, ctx_key, int(nxt), float(w)))

def prob_from_ngrams(ctx: List[int], candidate: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    row = query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key))
    tot = (row["w"] or 0.0) if row else 0.0
    if tot <= 0: return 0.0
    row2 = query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, candidate))
    w = (row2["weight"] or 0.0) if row2 else 0.0
    return w / tot

# =========================
# Parsers do canal (apenas para alimentar timeline/fechar pendências)
# =========================
GREEN_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bGREEN\b.*?\(([1-4])\)", re.I | re.S),
    re.compile(r"\bGREEN\b.*?Número[:\s]*([1-4])", re.I | re.S),
]
RED_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\(([1-4])\)", re.I | re.S),
    re.compile(r"\bLOSS\b.*?Número[:\s]*([1-4])", re.I | re.S),
]

def extract_green_number(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in GREEN_PATTERNS:
        m = rx.search(t)
        if m:
            return int(m.group(1))
    return None

def extract_red_last_left(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in RED_PATTERNS:
        m = rx.search(t)
        if m:
            return int(m.group(1))
    return None

def is_analise(text:str) -> bool:
    return bool(re.search(r"\bANALISANDO\b", text, flags=re.I))

# =========================
# Estatísticas
# =========================
def laplace_ratio(wins:int, losses:int) -> float:
    return (wins + 1.0) / (wins + losses + 2.0)

def update_daily_score(stage: Optional[int], won: bool):
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    if not row: g0=g1=g2=loss=streak=0
    else:       g0,g1,g2,loss,streak = row["g0"],row["g1"],row["g2"],row["loss"],row["streak"]

    if SCORE_G0_ONLY:
        if won and stage == 0: g0 += 1; streak += 1
        elif not won:          loss += 1; streak = 0
    else:
        if won:
            if stage == 0: g0 += 1
            elif stage == 1: g1 += 1
            elif stage == 2: g2 += 1
            streak += 1
        else:
            loss += 1; streak = 0

    exec_write("""
      INSERT INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
      VALUES (?,?,?,?,?,?)
      ON CONFLICT(yyyymmdd) DO UPDATE SET
        g0=excluded.g0, g1=excluded.g1, g2=excluded.g2, loss=excluded.loss, streak=excluded.streak
    """, (y,g0,g1,g2,loss,streak))

# Heurística extra: top-2 últimos K
def tail_top2_boost(tail: List[int], k:int=40) -> Dict[int, float]:
    boosts = {1:1.00, 2:1.00, 3:1.00, 4:1.00}
    if not tail: return boosts
    tail_k = tail[-k:] if len(tail) >= k else tail[:]
    c = Counter(tail_k)
    if not c: return boosts
    freq = c.most_common()
    if len(freq) >= 1: boosts[freq[0][0]] = 1.04
    if len(freq) >= 2: boosts[freq[1][0]] = 1.02
    return boosts

# =========================
# Predição
# =========================
def ngram_backoff_score(tail: List[int], candidate: int) -> float:
    score = 0.0
    if not tail: return 0.0
    ctx4 = tail[-4:] if len(tail) >= 4 else []
    ctx3 = tail[-3:] if len(tail) >= 3 else []
    ctx2 = tail[-2:] if len(tail) >= 2 else []
    ctx1 = tail[-1:] if len(tail) >= 1 else []
    parts = []
    if len(ctx4)==4: parts.append((W4, prob_from_ngrams(ctx4[:-1], candidate)))
    if len(ctx3)==3: parts.append((W3, prob_from_ngrams(ctx3[:-1], candidate)))
    if len(ctx2)==2: parts.append((W2, prob_from_ngrams(ctx2[:-1], candidate)))
    if len(ctx1)==1: parts.append((W1, prob_from_ngrams(ctx1[:-1], candidate)))
    for w,p in parts: score += w*p
    return score

def confident_best(post: Dict[int,float], gap: float = GAP_MIN) -> Optional[int]:
    a = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a: return None
    if len(a)==1: return a[0][0]
    return a[0][0] if (a[0][1]-a[1][1]) >= gap else None

def suggest_number(base: List[int]) -> Tuple[Optional[int],float,int,Dict[int,float]]:
    if not base: base = [1,2,3,4]
    tail = get_recent_tail(WINDOW)
    boosts = tail_top2_boost(tail, k=40)
    scores: Dict[int, float] = {}
    for c in base:
        ng = ngram_backoff_score(tail, c)
        prior = 1.0/len(base)
        score = (prior) * ((ng or 1e-6) ** ALPHA) * boosts.get(c,1.0)
        scores[c] = score
    total = sum(scores.values()) or 1e-9
    post = {k: v/total for k,v in scores.items()}
    number = confident_best(post, gap=GAP_MIN)
    conf = post.get(number, 0.0) if number is not None else 0.0
    # proxy de amostras (peso total acumulado)
    roww = query_one("SELECT SUM(weight) AS s FROM ngram_stats")
    samples = int((roww["s"] or 0) if roww else 0)
    # debouncing: se o último anunciado foi o mesmo nº e confiança caiu, abstenha
    last = query_one("SELECT suggested, announced FROM pending_outcome ORDER BY id DESC LIMIT 1")
    if last and number is not None and last["suggested"] == number and (last["announced"] or 0) == 1:
        if conf < (MIN_CONF_G0 + 0.08):
            return None, 0.0, samples, post
    # abster em empate técnico
    top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)[:2]
    if len(top) == 2 and (top[0][1] - top[1][1]) < 0.015:
        return None, 0.0, samples, post
    # checagens finais
    if samples < MIN_SAMPLES or number is None or conf < MIN_CONF_G0:
        return None, 0.0, samples, post
    return number, conf, samples, post

def build_suggestion_msg(number:int, conf:float, samples:int, stage:str="G0") -> str:
    return (
        f"🤖 <b>{SELF_LABEL_IA} [FIRE]</b>\n"
        f"🎯 Número seco ({stage}): <b>{number}</b>\n"
        f"📈 Conf: <b>{conf*100:.2f}%</b> | Amostra≈<b>{samples}</b>"
    )

# =========================
# IA loop
# =========================
_ia2_blocked_until_ts: int = 0
_ia2_sent_this_hour: int = 0
_ia2_hour_bucket: Optional[int] = None
_ia2_last_fire_ts: int = 0

def _ia2_hour_key() -> int: return int(datetime.now(timezone.utc).strftime("%Y%m%d%H"))
def _ia2_reset_hour():
    global _ia2_sent_this_hour, _ia2_hour_bucket
    hb = _ia2_hour_key()
    if _ia2_hour_bucket != hb:
        _ia2_hour_bucket = hb
        _ia2_sent_this_hour = 0
def _ia2_antispam_ok() -> bool:
    _ia2_reset_hour()
    return _ia2_sent_this_hour < IA2_MAX_PER_HOUR
def _ia2_mark_sent():
    global _ia2_sent_this_hour; _ia2_sent_this_hour += 1
def _ia2_blocked_now() -> bool: return now_ts() < _ia2_blocked_until_ts
def _ia_set_post_loss_block():
    global _ia2_blocked_until_ts; _ia2_blocked_until_ts = now_ts() + int(IA2_COOLDOWN_AFTER_LOSS)
def _ia2_can_fire_now() -> bool:
    if _ia2_blocked_now(): return False
    if not _ia2_antispam_ok(): return False
    if now_ts() - _ia2_last_fire_ts < IA2_MIN_SECONDS_BETWEEN_FIRE: return False
    return True
def _ia2_mark_fire_sent():
    global _ia2_last_fire_ts; _ia2_mark_sent(); _ia2_last_fire_ts = now_ts()

async def ia2_send_signal(best:int, conf:float, tail_len:int, mode:str):
    txt = build_suggestion_msg(best, conf, tail_len, stage="G0")
    await send_ia_text(txt)

def open_pending(strategy: Optional[str], suggested: int, source: str = "IA"):
    exec_write("""
        INSERT INTO pending_outcome
        (created_at,strategy,suggested,stage,open,window_left,seen_numbers,announced,source)
        VALUES (?,?,?,?,1,?, '', 1, ?)
    """, (now_ts(), strategy or "", int(suggested), 0, MAX_STAGE, source))

async def ia2_process_once():
    # IA avalia continuamente e dispara quando critérios são satisfeitos
    best, conf_raw, tail_len, post = suggest_number([1,2,3,4])
    if best is None:
        return
    gap = 1.0
    if post and len(post) >= 2:
        top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
        if len(top) >= 2: gap = top[0][1] - top[1][1]
    if _ia2_blocked_now(): return
    if not _ia2_antispam_ok(): return
    if now_ts() - _ia2_last_fire_ts < IA2_MIN_SECONDS_BETWEEN_FIRE: return
    if (conf_raw >= IA2_TIER_STRICT or (conf_raw >= IA2_TIER_STRICT - IA2_DELTA_GAP and gap >= IA2_GAP_SAFETY)) and tail_len >= MIN_SAMPLES and _ia2_can_fire_now():
        open_pending(None, best, source="IA")
        await ia2_send_signal(best, conf_raw, tail_len, "FIRE")
        _ia2_mark_fire_sent()

# =========================
# Fechamento de pendências
# =========================
async def close_pending_with_result(n_observed: int, event_kind: str):
    try:
        rows = query_all("""
            SELECT id, created_at, strategy, suggested, stage, open, window_left,
                   seen_numbers, announced, source
            FROM pending_outcome
            WHERE open=1
            ORDER BY id ASC
        """)
        if not rows: return {"ok": True, "no_open": True}

        for r in rows:
            pid        = r["id"]
            suggested  = int(r["suggested"])
            stage      = int(r["stage"])
            left       = int(r["window_left"])
            src        = (r["source"] or "IA").upper()
            seen       = (r["seen_numbers"] or "").strip()
            seen_new   = (seen + ("," if seen else ",") + str(int(n_observed)))

            if int(n_observed) == suggested:
                exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?", (seen_new, pid))
                update_daily_score(0 if stage==0 else stage, True)
                await send_green_channel(suggested, "G0" if stage==0 else f"G{stage}")
            else:
                if left > 1:
                    exec_write("""
                        UPDATE pending_outcome
                           SET stage = stage + 1, window_left = window_left - 1, seen_numbers=?
                         WHERE id=?
                    """, (seen_new, pid))
                    if stage == 0:
                        update_daily_score(0, False)
                        _ia_set_post_loss_block()
                        await send_loss_channel(suggested, "G0")
                else:
                    exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?", (seen_new, pid))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# =========================
# Webhook models & routes
# =========================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN> ou GET /ping"}

@app.get("/ping")
async def ping():
    try:
        await tg_broadcast("🔧 Bot vivo — IA independente pronta.")
        return {"ok": True, "to": SIGNAL_CHANNEL}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.on_event("startup")
async def _boot_tasks():
    async def _loop():
        while True:
            try: await ia2_process_once()
            except Exception as e: print(f"[IA2] analyzer error: {e}")
            await asyncio.sleep(0.25)
    try: asyncio.create_task(_loop())
    except Exception as e: print(f"[IA2] startup error: {e}")

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg: return {"ok": True}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    t = re.sub(r"\s+", " ", text)

    # GREEN/RED -> fecha pendências + timeline
    gnum = extract_green_number(t); redn = extract_red_last_left(t)
    if gnum is not None or redn is not None:
        n_observed = gnum if gnum is not None else redn
        append_timeline(n_observed); update_ngrams()
        await close_pending_with_result(n_observed, "GREEN" if gnum is not None else "RED")
        return {"ok": True, "observed": n_observed}

    # ANALISANDO -> apenas timeline (para alimentar IA)
    if is_analise(t):
        parts = re.findall(r"[1-4]", t)
        seq_left_recent = [int(x) for x in parts]
        seq_old_to_new  = seq_left_recent[::-1]  # chegam “da direita p/ esquerda”
        for n in seq_old_to_new: append_timeline(n)
        update_ngrams()
        return {"ok": True, "analise": True}

    # Entrada confirmada é ignorada como gatilho de decisão (IA é independente)
    return {"ok": True, "ignored": "entry_confirmed_or_other"}
