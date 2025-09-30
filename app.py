# app.py (MINIMAL)
import os
import logging
from fastapi import FastAPI, Request
import httpx

app = FastAPI()
log = logging.getLogger("uvicorn.error")

# Vars de ambiente obrigatórias (configure no Render → Environment)
TG_BOT_TOKEN    = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID  = int(os.environ["SOURCE_CHAT_ID"])   # ex: -1003156785631
TARGET_CHAT_ID  = int(os.environ["TARGET_CHAT_ID"])   # ex: -1002796105884
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

@app.get("/")
async def health():
    return {"ok": True, "service": "minimal-webhook"}

# Aceita /webhook, /webhook/ e qualquer sufixo (ex.: /webhook/segredo)
@app.api_route("/webhook{path:path}", methods=["GET", "POST"])
async def webhook_any(path: str, req: Request):
    # GET: só pra teste no navegador (evita 405/404)
    if req.method == "GET":
        return {"ok": True, "webhook": f"/webhook{path}", "method": "GET"}

    # POST: simula o Telegram
    update = await req.json()
    log.info("RAW UPDATE: %s", update)

    msg = update.get("message") or update.get("channel_post") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or ""

    # Se veio do canal de origem e tem texto, espelha para o destino
    if chat_id == SOURCE_CHAT_ID and text:
        async with httpx.AsyncClient(timeout=20) as cli:
            await cli.post(f"{API}/sendMessage",
                           json={"chat_id": TARGET_CHAT_ID, "text": text})
    return {"ok": True}