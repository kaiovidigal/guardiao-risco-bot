# main.py
# FastAPI + Webhook do Telegram (simples e direto)
# Repassa mensagens de um canal SOURCE_CHANNEL para TARGET_CHANNEL
# e expõe /set_webhook para configurar o webhook no Telegram.

import os
import httpx
from fastapi import FastAPI, Request, HTTPException, Query

app = FastAPI(title="telegram-webhook-min", version="1.0.0")

# --- Variáveis de ambiente ---
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "meusegredo123").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()   # opcional
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "").strip()   # obrigatório

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not WEBHOOK_TOKEN:
    raise RuntimeError("Defina WEBHOOK_TOKEN no ambiente.")
if not TARGET_CHANNEL:
    raise RuntimeError("Defina TARGET_CHANNEL no ambiente (ex.: -1002796105884).")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ---------- Utils ----------
async def tg_send_text(chat_id: str, text: str):
    """Envia texto simples pro Telegram."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        r.raise_for_status()

# ---------- Health ----------
@app.get("/")
async def root():
    return {"ok": True, "service": "telegram webhook (minimal)", "version": "1.0.0"}

@app.get("/health")
async def health():
    return {
        "ok": True,
        "has_token": bool(TG_BOT_TOKEN),
        "has_webhook_token": bool(WEBHOOK_TOKEN),
        "source": SOURCE_CHANNEL or "(any)",
        "target": TARGET_CHANNEL,
    }

# ---------- Setar Webhook (helper) ----------
@app.get("/set_webhook")
async def set_webhook(host: str = Query(..., description="Ex.: https://guardiao-risco-bot-2.onrender.com")):
    """
    Chame assim, depois do deploy:
    GET /set_webhook?host=https://SEU-APP.onrender.com
    """
    url = f"{host.rstrip('/')}/webhook/{WEBHOOK_TOKEN}"
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.get(f"{TELEGRAM_API}/setWebhook", params={"url": url})
        data = r.json()
    return {"requested_url": url, "telegram_response": data}

# ---------- Webhook (Telegram chama aqui) ----------
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden (bad token)")

    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # debug nos logs
    print("📩 Recebido:", update)

    msg = update.get("channel_post") or update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    if SOURCE_CHANNEL and chat_id != SOURCE_CHANNEL:
        return {"ok": True, "ignored": "not-from-source", "chat_id": chat_id}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return {"ok": True, "skipped": "no-text"}

    try:
        await tg_send_text(TARGET_CHANNEL, text)
        return {"ok": True, "relayed_to": TARGET_CHANNEL}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Telegram send failed: {e}")