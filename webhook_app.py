# -*- coding: utf-8 -*-
# Fan Tan — Guardião (G0 + Recuperação G1/G2) — IA Cauda(40) simplificada

import os, re, time, sqlite3, asyncio
from typing import Optional, List
from datetime import datetime, timezone, timedelta
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
REPL_CHANNEL   = os.getenv("REPL_CHANNEL", "-1003052132833").strip()
SELF_LABEL_IA  = os.getenv("SELF_LABEL_IA", "IA Cauda 40").strip()

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# =========================
# Parâmetros IA
# =========================
TAIL_FREQ_K = 40
MAX_STAGE = 3
MIN_SECONDS_BETWEEN_FIRE = 5
MAX_PER_HOUR = 60
COOLDOWN_AFTER_LOSS = 8

# =========================
# FASTAPI
# =========================
app = FastAPI(title="Fantan Guardião — Cauda(40)", version="1.0.0")

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
    con = _connect()
    con.execute(sql, params)
    con.commit()
    con.close()

def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    con = _connect()
    row = con.execute(sql, params).fetchone()
    con.close()
    return row

def query_all(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    con = _connect()
    rows = con.execute(sql, params).fetchall()
    con.close()
    return rows

def init_db():
    con = _connect()
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
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
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY,
        g0 INTEGER NOT NULL DEFAULT 0,
        g1 INTEGER NOT NULL DEFAULT 0,
        g2 INTEGER NOT NULL DEFAULT 0,
        loss INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0
    )""")
    con.commit()
    con.close()

init_db()

# =========================
# Utils
# =========================
def now_ts() -> int: return int(time.time())
def today_key() -> str: return datetime.now(timezone.utc).strftime("%Y%m%d")

async def tg_send(text: str):
    if not TG_BOT_TOKEN: return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": REPL_CHANNEL, "text": text, "parse_mode": "HTML"})

# =========================
# Timeline helpers
# =========================
def append_timeline(n: int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_tail(k: int = TAIL_FREQ_K) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (k,))
    return [r["number"] for r in rows][::-1]

# =========================
# Placar
# =========================
def update_daily_score(stage: int, won: bool):
    y = today_key()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0,g1,g2,loss,streak = (row["g0"],row["g1"],row["g2"],row["loss"],row["streak"]) if row else (0,0,0,0,0)
    if won:
        if stage == 0: g0+=1
        elif stage == 1: g1+=1
        else: g2+=1
        streak+=1
    else:
        loss+=1; streak=0
    exec_write("""INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
                  VALUES (?,?,?,?,?,?)""",(y,g0,g1,g2,loss,streak))

# =========================
# IA Loop simplificado
# =========================
IA_LAST_REASON = "—"
IA_LAST_TS = 0

async def ia_loop_once():
    global IA_LAST_REASON, IA_LAST_TS
    tail = get_tail(TAIL_FREQ_K)
    if not tail: return
    c = Counter(tail)
    choice, _ = c.most_common(1)[0]
    append_timeline(choice)  # reforça
    update_daily_score(0, True)  # simulação rápida
    await tg_send(f"🤖 <b>{SELF_LABEL_IA}</b>\n🎯 Número seco (G0): <b>{choice}</b>")
    IA_LAST_REASON="fire"; IA_LAST_TS=now_ts()

async def _ia_runner():
    while True:
        try: await ia_loop_once()
        except Exception as e: print(f"[IA] {e}")
        await asyncio.sleep(5)

# =========================
# Webhook
# =========================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None

@app.on_event("startup")
async def _boot():
    asyncio.create_task(_ia_runner())

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN: raise HTTPException(status_code=403)
    return {"ok": True}

@app.get("/debug/reason")
async def debug_reason():
    return {"last_reason": IA_LAST_REASON, "age": now_ts()-IA_LAST_TS}
