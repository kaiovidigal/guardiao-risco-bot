import os
import json
import httpx
from fastapi import FastAPI, Request, HTTPException

app = FastAPI(title="telegram-webhook-min", version="1.0.0")

TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "").strip()

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not TARGET_CHANNEL:
    raise RuntimeError("Defina TARGET_CHANNEL no ambiente.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

async def tg_send_text(chat_id: str, text: str):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        r.raise_for_status()

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    msg = update.get("channel_post") or update.get("message") or {}
    text = msg.get("text") or msg.get("caption")
    if text:
        await tg_send_text(TARGET_CHANNEL, text)
    return {"ok": True}