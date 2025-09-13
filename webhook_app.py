# -*- coding: utf-8 -*-
# Fan Tan — Guardião (com 4 cards fixos para contagem "X vezes sem vir")

import os, re, json, time, sqlite3, asyncio, httpx
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from fastapi import FastAPI

# ====================================
# CONFIG
# ====================================
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()

REPL_ENABLED  = True
REPL_CHANNEL  = os.getenv("REPL_CHANNEL", "-100xxxxxxxxxxxx")  # id do canal espelho

DB_PATH = os.getenv("DB_PATH", "/data/data.db")
MISS_ALERT_MIN = int(os.getenv("MISS_ALERT_MIN", "10"))

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI(title="Fantan Guardião com Painel", version="1.0.0")

# ====================================
# DB Helpers
# ====================================
def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    return con

def exec_write(sql: str, params: tuple = ()):
    con = _connect()
    con.execute(sql, params)
    con.commit()
    con.close()

def query_one(sql: str, params: tuple = ()):
    con = _connect()
    row = con.execute(sql, params).fetchone()
    con.close()
    return row

def query_all(sql: str, params: tuple = ()):
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
    cur.execute("""CREATE TABLE IF NOT EXISTS streak_msgs (
        number INTEGER PRIMARY KEY,
        message_id INTEGER NOT NULL,
        last_value INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL
    )""")
    con.commit()
    con.close()

init_db()

# ====================================
# Utils
# ====================================
def now_ts() -> int:
    return int(time.time())

def get_recent_tail(window:int=2000):
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def get_miss_counts(window:int=2000) -> Dict[int, Dict[str, Any]]:
    tail = get_recent_tail(window)
    res: Dict[int, Dict[str, Any]] = {}
    rows = query_all("SELECT number, MAX(created_at) AS ts FROM timeline GROUP BY number")
    last_ts = {int(r["number"]): int(r["ts"] or 0) for r in rows}

    for num in (1,2,3,4):
        miss = 0
        for n in reversed(tail):  # mais recente → mais antigo
            if n == num:
                break
            miss += 1
        res[num] = {"miss": miss, "last_seen_ts": last_ts.get(num)}
    return res

def _ago_str(ts: Optional[int]) -> str:
    if not ts:
        return "nunca visto"
    delta = max(0, now_ts() - ts)
    mins = delta // 60
    hours = mins // 60
    if hours >= 1:
        return f"há {hours}h{mins%60:02d}min"
    elif mins >= 1:
        return f"há {mins}min"
    else:
        return "agora há pouco"

def _panel_text_for_number(num:int, miss:int, last_ts: Optional[int]) -> str:
    if miss == 0:
        return (f"🧮 <b>Fantan — Card {num}</b>\n"
                f"✅ Saiu agora (zerado)\n"
                f"🕒 Último: {_ago_str(last_ts)}")
    if miss >= MISS_ALERT_MIN:
        return (f"⚠️ <b>Atenção!</b>\n"
                f"Número <b>{num}</b> está <b>{miss}</b> rodadas sem vir\n"
                f"🕒 Último: {_ago_str(last_ts)}\n"
                f"🎯 Alerta ≥ {MISS_ALERT_MIN}")
    return (f"🧮 <b>Fantan — Card {num}</b>\n"
            f"{miss} sem vir (abaixo do alerta)\n"
            f"🕒 Último: {_ago_str(last_ts)}")

# ====================================
# Telegram helpers
# ====================================
async def tg_edit_message_text(chat_id: str, message_id: int, text: str, parse: str="HTML"):
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{TELEGRAM_API}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse},
        )

async def init_streak_msgs():
    """Cria as 4 mensagens fixas (1..4) se não existirem."""
    if not REPL_ENABLED or not REPL_CHANNEL:
        return
    for num in (1,2,3,4):
        row = query_one("SELECT message_id FROM streak_msgs WHERE number=?", (num,))
        if row:
            continue
        txt = f"⏳ Inicializando card do número {num}..."
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": REPL_CHANNEL, "text": txt, "parse_mode": "HTML"},
            )
            try:
                mid = r.json().get("result", {}).get("message_id")
            except Exception:
                mid = None
        if mid:
            exec_write("INSERT OR REPLACE INTO streak_msgs (number,message_id,last_value,updated_at) VALUES (?,?,?,?)",
                       (num, mid, 0, now_ts()))

async def update_miss_panels():
    """Recalcula e edita os 4 cards fixos."""
    if not REPL_ENABLED or not REPL_CHANNEL:
        return
    data = get_miss_counts()
    for num in (1,2,3,4):
        row = query_one("SELECT message_id,last_value FROM streak_msgs WHERE number=?", (num,))
        if not row:
            continue
        mid   = int(row["message_id"])
        lastv = int(row["last_value"] or 0)
        miss  = int(data.get(num, {}).get("miss", 0))
        last_ts = data.get(num, {}).get("last_seen_ts")

        if miss != lastv:
            txt = _panel_text_for_number(num, miss, last_ts)
            await tg_edit_message_text(REPL_CHANNEL, mid, txt)
            exec_write("UPDATE streak_msgs SET last_value=?, updated_at=? WHERE number=?",
                       (miss, now_ts(), num))

# ====================================
# Startup
# ====================================
@app.on_event("startup")
async def startup_event():
    await init_streak_msgs()
    async def loop_panels():
        while True:
            try:
                await update_miss_panels()
            except Exception as e:
                print(f"[PANELS] erro: {e}")
            await asyncio.sleep(5)
    asyncio.create_task(loop_panels())

# ====================================
# Endpoints
# ====================================
@app.get("/")
async def root():
    return {"ok": True}