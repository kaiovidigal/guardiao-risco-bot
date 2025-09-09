# -*- coding: utf-8 -*-
# webhook_app.py — Guardião Auto Fantan (com retroativo e ingest)

import os, re, json, time, logging, math
from typing import Dict, Tuple, List, Optional
from collections import defaultdict, deque

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from datetime import datetime

# 🔹 NOVO: carregar variáveis do .env
from dotenv import load_dotenv
load_dotenv()

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("fantan-auto")

# =========================
# VARIÁVEIS DE AMBIENTE
# =========================
BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "")
CHANNEL_ID  = int(os.getenv("CHANNEL_ID", "0"))
PUBLIC_URL  = (os.getenv("PUBLIC_URL") or "").rstrip("/")
SESSION_STRING = os.getenv("SESSION_STRING", "")

if not BOT_TOKEN:
    raise RuntimeError("⚠️ Faltando TG_BOT_TOKEN no .env ou variáveis de ambiente.")

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp  = Dispatcher(bot)
app = FastAPI()

# =========================
# ESTADO
# =========================
STATE_FILE = "data/state.json"
os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
state = {"greens_total": 0, "reds_total": 0, "sinais_enviados": 0}

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            state.update(json.load(open(STATE_FILE, "r", encoding="utf-8")))
    except:
        pass

def save_state():
    try:
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except:
        pass

load_state()

# =========================
# HANDLERS TELEGRAM
# =========================
@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    await msg.answer("🤖 Guardião Auto iniciado e pronto!", parse_mode="HTML")

@dp.message_handler(commands=["status"])
async def cmd_status(msg: types.Message):
    total = state.get("greens_total", 0) + state.get("reds_total", 0)
    winrate = (state.get("greens_total", 0) / total * 100) if total else 0
    await msg.answer(
        f"📊 Status:\n✅ Greens: {state['greens_total']}\n❌ Reds: {state['reds_total']}\n"
        f"🎯 Winrate: {winrate:.1f}%", parse_mode="HTML"
    )

# =========================
# INGEST API (manual)
# =========================
class IngestItem(BaseModel):
    id: Optional[int] = None
    date: Optional[str] = None
    text: str

class IngestPayload(BaseModel):
    items: List[IngestItem]

@app.post("/ingest")
async def ingest(payload: IngestPayload):
    added = 0
    for it in payload.items:
        if "GREEN" in it.text.upper():
            state["greens_total"] += 1
            added += 1
        elif "RED" in it.text.upper():
            state["reds_total"] += 1
            added += 1
    save_state()
    return {"ok": True, "added": added}

# =========================
# SCRAPER AUTOMÁTICO (retroativo)
# =========================
if SESSION_STRING:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    API_ID = int(os.getenv("API_ID", "0"))
    API_HASH = os.getenv("API_HASH", "")

    if API_ID and API_HASH:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

        @app.on_event("startup")
        async def startup_event():
            await client.start()
            log.info("📥 Retroativo conectado no Telegram com Telethon")

            async for msg in client.iter_messages(CHANNEL_ID, limit=100):
                if "GREEN" in (msg.text or "").upper():
                    state["greens_total"] += 1
                elif "RED" in (msg.text or "").upper():
                    state["reds_total"] += 1
            save_state()
            log.info("✅ Retroativo inicial concluído.")

# =========================
# WEBHOOK TELEGRAM
# =========================
@app.on_event("startup")
async def on_startup():
    if PUBLIC_URL:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(f"{PUBLIC_URL}/webhook/{BOT_TOKEN}")
        log.info("🌍 Webhook registrado em %s/webhook/%s", PUBLIC_URL, BOT_TOKEN)
    else:
        log.warning("⚠️ PUBLIC_URL não definido, bot pode não receber updates.")

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.process_update(update)
    return {"ok": True}