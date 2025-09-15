from fastapi import FastAPI, Request
from api_fanta import get_latest_result
import requests

app = FastAPI(title="Guardião Risco Bot")

# Configurações do bot
TELEGRAM_TOKEN = "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4"
CHAT_ID = "-1003052132833"  # seu grupo/canal no Telegram

# --- HEALTH CHECK ---
@app.get("/health")
async def health():
    return {"ok": True}

# --- ROTA PRINCIPAL (FanTan API) ---
@app.get("/")
async def root():
    result = await get_latest_result()
    if result:
        numero, ts_epoch = result
        return {"numero": numero, "timestamp": ts_epoch}
    return {"erro": "Nenhum resultado válido encontrado"}

# --- ROTA DE WEBHOOK (para receber mensagens externas) ---
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Mensagem recebida:", data)  # aparece nos logs do Render
    return {"ok": True}

# --- ROTA DE TESTE DE ENVIO PARA TELEGRAM ---
@app.get("/send")
async def send_message():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": "✅ Teste funcionando no canal! 🚀"
    }
    r = requests.post(url, json=payload)
    return {"status": r.json()}