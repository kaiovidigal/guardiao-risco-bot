from fastapi import FastAPI, Request
import requests

app = FastAPI()

TELEGRAM_TOKEN = "8217345207:AAEf5DjyRgIzxtDlTZVJX5bOjLw-uSg_i5o"
CHAT_ID = "-1003052132833"  # seu canal/grupo

@app.get("/")
async def root():
    return {"status": "online"}

@app.post("/webhook/" + TELEGRAM_TOKEN)
async def webhook(request: Request):
    data = await request.json()
    print("Mensagem recebida:", data)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": f"📩 Novo sinal recebido:\n\n{data}"
    }
    requests.post(url, json=payload)
    return {"ok": True}

@app.get("/send")
async def send_message():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": "✅ Teste funcionando! 🚀"
    }
    r = requests.post(url, json=payload)
    return {"status": r.json()}