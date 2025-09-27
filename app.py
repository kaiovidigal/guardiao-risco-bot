import os
import requests
from fastapi import FastAPI, Request

# Configurações
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "meusegredo123")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4")
DEST_CHANNEL_ID = int(os.getenv("DEST_CHANNEL_ID", "-1002810508717"))  # id do canal de destino

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()


@app.get("/health")
async def health():
    return {"ok": True}


@app.post(f"/webhook/{WEBHOOK_TOKEN}")
async def telegram_webhook(request: Request):
    update = await request.json()
    print("📩 Recebido:", update)  # vai aparecer nos logs da Render

    try:
        data = update.get("channel_post") or update.get("message") or {}
        chat = data.get("chat", {})
        chat_id = int(chat.get("id", 0))
        text = data.get("text") or ""

        if not chat_id or not text:
            return {"ok": True}

        # Não reposta para o próprio canal de destino
        if chat_id == DEST_CHANNEL_ID:
            return {"ok": True}

        # Filtro simples: só repassa se parecer sinal
        if "🚨" in text or "ENTRADA CONFIRMADA" in text or "ANALISANDO" in text:
            resp = requests.post(f"{TELEGRAM_API}/sendMessage", json={
                "chat_id": DEST_CHANNEL_ID,
                "text": text
            }, timeout=15)

            print("➡️ Repassado:", resp.text)

    except Exception as e:
        print("⚠️ Erro no handle:", e)

    return {"ok": True}