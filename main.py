from fastapi import FastAPI, Request
import requests
import os
from api_fanta import get_latest_result

app = FastAPI(title="Guardião Risco Bot")

# Configurações do bot Telegram
TELEGRAM_TOKEN = "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4"
CHAT_ID = "-1003052132833"  # seu grupo/canal do Telegram


# ---------- ROTAS DE STATUS ----------
@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/")
async def root():
    result = await get_latest_result()
    if result:
        numero, ts_epoch = result
        return {"numero": numero, "timestamp": ts_epoch}
    return {"erro": "Nenhum resultado válido encontrado"}


# ---------- ROTAS DO TELEGRAM ----------
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Mensagem recebida:", data)  # aparece nos logs do Render
    return {"ok": True}


@app.get("/send")
async def send_message():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": "✅ Teste funcionando no canal! 🚀"
    }
    r = requests.post(url, json=payload)
    return {"status": r.json()}