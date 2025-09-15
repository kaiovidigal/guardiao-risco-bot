from fastapi import FastAPI
import requests

app = FastAPI()

# Configurações do bot
TELEGRAM_TOKEN = "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4"
CHAT_ID = "-1003052132833"  # canal/grupo destino

# --- HEALTH CHECK ---
@app.get("/health")
async def health():
    return {"ok": True}


# --- ROTA PARA ENVIAR SINAL MANUAL ---
@app.get("/send")
async def send_message(text: str = "✅ Teste funcionando no canal destino! 🚀"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text
    }
    r = requests.post(url, json=payload)
    return {"status": r.json()}