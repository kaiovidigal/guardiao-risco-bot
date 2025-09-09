# -*- coding: utf-8 -*-
import os, re, json, time, sqlite3
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
DB_PATH        = "/var/data/data.db"  # local correto para persistência no Render
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()   # -100... ou @canal
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()

if not TG_BOT_TOKEN or not PUBLIC_CHANNEL or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN, PUBLIC_CHANNEL e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Canal de réplica para sinais G0
REPL_ENABLED  = True
REPL_CHANNEL  = -1003052132833

# Hiperparâmetros
WINDOW = 400
DECAY  = 0.985
W4, W3, W2, W1 = 0.45, 0.30, 0.17, 0.08
ALPHA, BETA, GAMMA = 1.10, 0.65, 0.35
GAP_MIN = 0.08

app = FastAPI(title="Guardião FanTan", version="3.0.0")

# =========================
# Banco de dados SQLite
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

# =========================
# Inicialização do banco
# =========================
def init_db():
    con = _connect()
    cur = con.cursor()
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
        seen_numbers TEXT DEFAULT ''
    )""")
    con.commit()
    con.close()

init_db()

# =========================
# Utilitários
# =========================
def now_ts() -> int:
    return int(time.time())

def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")

async def tg_send_text(chat_id, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id or not text:
        return
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True},
            )
        except Exception as e:
            print(f"[TG] Erro ao enviar mensagem: {e}")

async def tg_replicar_sinal(text: str):
    if not REPL_ENABLED or not REPL_CHANNEL or not text:
        return
    await tg_send_text(REPL_CHANNEL, text, parse="HTML")

# =========================
# Filtros de mensagem
# =========================
MUST_HAVE = (
    r"ENTRADA\s+CONFIRMADA",
    r"Mesa:\s*.*?Fantan.*?Evolution",
)
MUST_NOT  = (r"\bANALISANDO\b", r"\bPlacar do dia\b", r"\bAPOSTA ENCERRADA\b")

def is_real_entry(text: str) -> bool:
    t = re.sub(r"\s+", " ", text).strip()
    for bad in MUST_NOT:
        if re.search(bad, t, flags=re.I):
            return False
    return all(re.search(g, t, flags=re.I|re.S) for g in MUST_HAVE)

def extract_after_num(text: str) -> Optional[int]:
    m = re.search(r"ap[oó]s\s*(?:o\s*)?([1-4])\b", text, flags=re.I)
    return int(m.group(1)) if m else None

def parse_bases_and_pattern(text: str) -> Tuple[List[int], str]:
    t = re.sub(r"\s+", " ", text).strip()

    if re.search(r"\bODD\b", t, flags=re.I):
        return [1, 3], "ODD"
    if re.search(r"\bEVEN\b", t, flags=re.I):
        return [2, 4], "EVEN"

    m = re.search(r"\bKWOK\s*([1-4])\s*-\s*([1-4])", t, flags=re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return [a, b], f"KWOK-{a}-{b}"

    m = re.search(r"\bSS?H\s*([1-4])(?:-([1-4]))?(?:-([1-4]))?(?:-([1-4]))?", t, flags=re.I)
    if m:
        nums = [int(g) for g in m.groups() if g]
        return nums, "SSH-" + "-".join(str(x) for x in nums)

    m = re.search(r"Sequ[eê]ncia:\s*([\d\s\|\-]+)", t, flags=re.I)
    if m:
        parts = re.findall(r"[1-4]", m.group(1))
        base, seen = [], set()
        for x in parts:
            xi = int(x)
            if xi not in seen:
                seen.add(xi); base.append(xi)
            if len(base) == 3: break
        if base:
            return base, "SEQ"

    return [], "GEN"

# =========================
# Resultado imediato
# =========================
async def resultado_green(sugerido:int, stage:int):
    msg = f"✅ <b>GREEN imediato</b> (G{stage}) → Número: <b>{sugerido}</b>"
    await tg_send_text(PUBLIC_CHANNEL, msg)

async def resultado_loss(sugerido:int):
    msg = f"❌ <b>LOSS imediato</b> → Número sugerido: <b>{sugerido}</b>"
    await tg_send_text(PUBLIC_CHANNEL, msg)

# =========================
# Rotas
# =========================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

@app.get("/")
async def root():
    return {"ok": True, "detail": "Webhook ativo"}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")

    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg: return {"ok": True}

    texto = (msg.get("text") or msg.get("caption") or "").strip()

    # Exemplo: só envia sinal confirmado
    if is_real_entry(texto):
        await tg_send_text(PUBLIC_CHANNEL, f"🎯 SINAL DETECTADO:\n{texto}")
        await tg_replicar_sinal(f"📡 Réplica:\n{texto}")

    return {"ok": True}