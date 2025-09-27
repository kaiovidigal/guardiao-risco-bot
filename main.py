# app.py
import os
import httpx
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "meusegredo123").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "").strip()

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not TARGET_CHANNEL:
    raise RuntimeError("Defina TARGET_CHANNEL no ambiente.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# --- Health ---
@app.get("/health")
async def health():
    return {"ok": True}

# --- Webhook ---
@app.post(f"/webhook/{WEBHOOK_TOKEN}")
async def telegram_webhook(request: Request):
    update = await request.json()
    print("📩 Recebido:", update)

    msg = update.get("channel_post") or update.get("message") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if not text:
        return {"ok": True, "skip": "no-text"}

    async with httpx.AsyncClient() as client:
        await client.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": TARGET_CHANNEL,
            "text": text
        })

    return {"ok": True, "relayed": TARGET_CHANNEL}