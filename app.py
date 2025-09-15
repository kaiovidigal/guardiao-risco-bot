from fastapi import FastAPI, Request
import requests

app = FastAPI()

# Configurações do bot
TELEGRAM_TOKEN = "8217345207:AAEf5DjyRgIzxtDlTZVJX5bOjLw-uSg_i5o"
CHAT_ID = "-1003052132833"  # seu grupo/canal no Telegram

# --- ROTA DE WEBHOOK ---
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Mensagem recebida:", data)  # só para debug nos logs do Render
    return {"ok": True}


# --- ROTA DE TESTE DE ENVIO ---
@app.get("/send")
async def send_message():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": "✅ Teste funcionando no canal! 🚀"
    }
    r = requests.post(url, json=payload)
    return {"status": r.json()}
