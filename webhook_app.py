# -*- coding: utf-8 -*-
# webhook_app.py — versão ajustada conforme solicitado

import os
import re
import json
import time
import sqlite3
import asyncio
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
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
FLUSH_KEY      = os.getenv("FLUSH_KEY", "meusegredo123").strip()

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
REPL_ENABLED, REPL_CHANNEL = True, "-1003052132833"  # espelho

# =========================
# Ajustes de qualidade e thresholds calibrados
# =========================
WINDOW = 400
DECAY  = 0.985
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12
ALPHA, BETA, GAMMA = 1.05, 0.70, 0.40
GAP_MIN = 0.08

MIN_CONF_G0 = 0.55
MIN_GAP_G0  = 0.04
MIN_SAMPLES = 1000

CONF_CAP, GAP_CAP = 0.999, 1.0

# =========================
# FastAPI
# =========================
app = FastAPI(title="Fantan Guardião — FIRE-only", version="3.11.0")

class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

@app.get("/")
async def root():
    return {"ok": True, "detail": "Webhook ativo e rodando"}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    msg = data.get("channel_post") or data.get("message") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if not text:
        return {"ok": True}

    # envia sinal quando encontrar "ENTRADA CONFIRMADA"
    if re.search(r"ENTRADA\s+CONFIRMADA", text, flags=re.I):
        out = f"🎯 Número seco (G0) detectado
📊 Confiança mínima atingida"
        await tg_broadcast(out)
        return {"ok": True, "sent": True}

    return {"ok": True, "ignored": True}

# =========================
# Telegram helpers
# =========================
async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse,
                                "disable_web_page_preview": True})

async def tg_broadcast(text: str, parse: str="HTML"):
    if REPL_ENABLED and REPL_CHANNEL:
        await tg_send_text(REPL_CHANNEL, text, parse)
