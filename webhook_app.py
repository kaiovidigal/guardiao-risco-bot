#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webhook_app.py — webhook mínimo para Telegram

- Rota exata: /webhook/{WEBHOOK_TOKEN}
- Healthcheck em / e /health
- (Opcional) Repassa mensagens do SOURCE_CHANNEL -> TARGET_CHANNEL

ENV obrigatórias:
  TG_BOT_TOKEN     = token do bot (ex: 123456:ABC...)
  WEBHOOK_TOKEN    = segredo do path (ex: meusegredo123)

ENV opcionais:
  SOURCE_CHANNEL   = id do canal origem (ex: -1003052132833)
  TARGET_CHANNEL   = id do canal destino (ex: -1002796105884)
  TZ_NAME          = America/Sao_Paulo (apenas para logs)
"""
import os
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI, Request, HTTPException

# ==== ENV ====
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()  # opcional
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "").strip()  # opcional
TZ_NAME        = os.getenv("TZ_NAME", "America/Sao_Paulo").strip()

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not WEBHOOK_TOKEN:
    raise RuntimeError("Defina WEBHOOK_TOKEN no ambiente.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ==== APP ====
app = FastAPI(title="webhook-telegram-minimal", version="1.0.0")

def now_str() -> str:
    try:
        return datetime.now(ZoneInfo(TZ_NAME)).isoformat(timespec="seconds")
    except Exception:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"

async def tg_send_text(chat_id: str, text: str):
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})

@app.get("/")
async def root():
    return {"ok": True, "service": "webhook-telegram-minimal", "time": now_str()}

@app.get("/health")
async def health():
    return {
        "ok": True,
        "time": now_str(),
        "source": SOURCE_CHANNEL or "(não filtrando)",
        "target": TARGET_CHANNEL or "(sem repasse)"
    }

# Rota EXATA que o Telegram vai chamar:
@app.post(f"/webhook/{WEBHOOK_TOKEN}")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    # Log no Render
    print(f"[{now_str()}] update: {str(data)[:800]}")

    # Pega mensagem ou channel_post
    msg = data.get("channel_post") or data.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    text = (msg.get("text") or msg.get("caption") or "").strip()

    # Se quiser filtrar por SOURCE_CHANNEL (opcional)
    if SOURCE_CHANNEL and chat_id and chat_id != SOURCE_CHANNEL:
        # Apenas ignora updates de outros chats
        return {"ok": True, "skipped": "other_chat"}

    # Se quiser repassar (opcional: exige TARGET_CHANNEL)
    if TARGET_CHANNEL and text:
        try:
            await tg_send_text(TARGET_CHANNEL, text)
        except Exception as e:
            print(f"[{now_str()}] erro ao repassar: {e}")

    # Sempre responda 200/OK para o Telegram
    return {"ok": True}