from fastapi import FastAPI, Request
import httpx
import os

app = FastAPI()

# 🔑 Configurações fixas
TELEGRAM_TOKEN = "8217345207:AAEf5DjyRgIzxtDlTZVJX5bOjLw-uSg_i5o"
SECRET_KEY = "meusegredo123"

# 👥 Canal ou grupo de destino
CHAT_ID = -1001234567890  # troque pelo ID real do grupo/canal

# 🚀 Rota healthcheck
@app.get("/health")
async def health():
    return {"ok": True}

# 🚀 Rota webhook protegida
@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != SECRET_KEY:
        return {"error": "Token inválido"}

    data = await request.json()

    text = data.get("text", "⚠️ Sinal recebido sem texto")
    msg = f"📢 *Novo Sinal Recebido:*\n\n{text}"

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    async with httpx.AsyncClient() as client:
        r = await client.post(url, json={
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        })

    return {"status": "enviado", "telegram_response": r.json()}