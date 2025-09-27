#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Webhook mínimo e robusto (fixo) para Telegram
- Configurações 100% embutidas (sem ENV)
- Dedupe por update_id
- Filtro por SOURCE_CHANNEL
- Encaminha texto para TARGET_CHANNEL

Rotas:
  GET  /                -> ok
  GET  /health          -> status básico
  POST /webhook/meusegredo123  -> endpoint do Telegram (FIXO)

Start:
  uvicorn webhook_app:app --host 0.0.0.0 --port $PORT
"""

import os
import sqlite3
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request

# ========= CONFIG FIXA (edite aqui se precisar) =========
TG_BOT_TOKEN   = "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4"
WEBHOOK_TOKEN  = "meusegredo123"          # rota secreta do webhook (fixa na URL)
TARGET_CHANNEL = "-1002796105884"         # destino (canal seu)
SOURCE_CHANNEL = "-1002810508717"         # fonte (canal monitorado)
DB_PATH        = "/var/data/data.db"
TZ_NAME        = "America/Sao_Paulo"

# Flags
DEBUG_MSG      = False
BYPASS_SOURCE  = False  # se True, ignora filtro de SOURCE_CHANNEL

# Endpoint Telegram API
TELEGRAM_API   = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ========= Utilidades =========
def now_ts() -> int:
    return int(time.time())

def ts_str(ts: int | None = None) -> str:
    if ts is None:
        ts = now_ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=10000;")
    return con

def _migrate():
    con = _connect()
    cur = con.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS processed (
        update_id TEXT PRIMARY KEY,
        seen_at   INTEGER NOT NULL
      )
    """)
    con.commit()
    con.close()

_migrate()

def _is_processed(update_id: str) -> bool:
    if not update_id:
        return False
    con = _connect()
    row = con.execute("SELECT 1 FROM processed WHERE update_id=?", (update_id,)).fetchone()
    con.close()
    return bool(row)

def _mark_processed(update_id: str):
    if not update_id:
        return
    con = _connect()
    try:
        con.execute("INSERT OR IGNORE INTO processed (update_id, seen_at) VALUES (?,?)",
                    (update_id, now_ts()))
        con.commit()
    finally:
        con.close()

async def tg_send_text(chat_id: str, text: str, parse: str = "HTML"):
    if not TG_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse,
                    "disable_web_page_preview": True,
                },
            )
    except Exception:
        # não derruba o webhook por erro de rede
        pass

# ========= App =========
app = FastAPI(title="guardiao-webhook-fixo", version="1.0.0")

@app.get("/")
async def root():
    return {"ok": True, "service": "guardiao-webhook-fixo", "time": ts_str()}

@app.get("/health")
async def health():
    return {
        "ok": True,
        "db": DB_PATH,
        "tz": TZ_NAME,
        "time": ts_str(),
        "source_channel": SOURCE_CHANNEL,
        "target_channel": TARGET_CHANNEL,
        "bypass_source": BYPASS_SOURCE,
    }

# ========= Webhook (rota fixa com token embutido) =========
@app.post(f"/webhook/{WEBHOOK_TOKEN}")
async def webhook(request: Request):
    data = await request.json()

    # Dedupe por update_id
    upd_id = ""
    if isinstance(data, dict):
        upd_id = str(data.get("update_id", "") or "")
    if upd_id and _is_processed(upd_id):
        return {"ok": True, "skipped": "duplicate_update"}
    if upd_id:
        _mark_processed(upd_id)

    # Extrai mensagem do Telegram (canal/mensagem/edição)
    msg = data.get("channel_post") or data.get("message") \
        or data.get("edited_channel_post") or data.get("edited_message") or {}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    # Filtro de origem (a menos que BYPASS_SOURCE=True)
    if SOURCE_CHANNEL and not BYPASS_SOURCE and chat_id != str(SOURCE_CHANNEL):
        if DEBUG_MSG:
            await tg_send_text(
                TARGET_CHANNEL,
                f"DEBUG: Ignorando chat {chat_id}. Esperado: {SOURCE_CHANNEL}",
            )
        return {"ok": True, "skipped": "wrong_source"}

    if not text:
        return {"ok": True, "skipped": "no_text"}

    # Encaminha para o canal destino
    await tg_send_text(TARGET_CHANNEL, text)
    return {"ok": True, "forwarded": True}