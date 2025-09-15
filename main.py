from fastapi import FastAPI, Request
import requests

app = FastAPI()

# Token do bot
TELEGRAM_TOKEN = "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4"
# Canal destino
CHAT_ID = "-1003052132833"

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Mensagem recebida:", data)  # aparece nos logs

    # --- Reenvio para o canal destino ---
    if "channel_post" in data:  # pega mensagens vindas do canal
        text = data["channel_post"].get("text", "")
        if text:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {
                "chat_id": CHAT_ID,
                "text": text
            }
            requests.post(url, json=payload)

    return {"ok": True}

@app.get("/send")
async def send_message():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": "✅ Teste funcionando no canal destino! 🚀"
    }
    r = requests.post(url, json=payload)
    return {"status": r.json()}