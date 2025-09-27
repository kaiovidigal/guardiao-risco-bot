import os, json, httpx
from fastapi import FastAPI, Request, HTTPException, Query

app = FastAPI(title="telegram-webhook-min", version="1.0.0")

TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "meusegredo123").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()   # ex: -1002810508717 (opcional)
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "").strip()   # ex: -1002796105884 (obrigatório)

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not TARGET_CHANNEL:
    raise RuntimeError("Defina TARGET_CHANNEL no ambiente.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

async def tg_send_text(chat_id: str, text: str):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        r.raise_for_status()

@app.get("/health")
async def health():
    return {"ok": True, "webhook_token": WEBHOOK_TOKEN, "source": SOURCE_CHANNEL or "(any)", "target": TARGET_CHANNEL}

@app.get("/set_webhook")
async def set_webhook(host: str):
    url = f"{host.rstrip('/')}/webhook/{WEBHOOK_TOKEN}"
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.get(f"{TELEGRAM_API}/setWebhook", params={"url": url})
    return {"requested_url": url, "telegram_response": r.json()}

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden (bad token)")
    update = await request.json()
    msg = update.get("channel_post") or update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if SOURCE_CHANNEL and chat_id != SOURCE_CHANNEL:
        return {"ok": True, "ignored": "not-from-source", "chat_id": chat_id}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return {"ok": True, "skipped": "no-text"}
    await tg_send_text(TARGET_CHANNEL, text)
    return {"ok": True, "relayed_to": TARGET_CHANNEL}