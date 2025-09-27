import os
import httpx
from fastapi import FastAPI, Request, HTTPException

# Configurações vindas das variáveis de ambiente
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "meusegredo123").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "").strip()

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente")
if not TARGET_CHANNEL:
    raise RuntimeError("Defina TARGET_CHANNEL no ambiente")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI()

# ---------- Health ----------
@app.get("/health")
async def health():
    return {"ok": True}

# ---------- Webhook ----------
@app.post(f"/webhook/{WEBHOOK_TOKEN}")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    msg = update.get("channel_post") or update.get("message") or {}
    text = msg.get("text") or msg.get("caption") or ""
    if not text:
        return {"ok": True, "skipped": "no-text"}

    async with httpx.AsyncClient(timeout=15) as cli:
        await cli.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": TARGET_CHANNEL,
            "text": text,
            "parse_mode": "HTML"
        })

    return {"ok": True}