from fastapi import FastAPI, Request
import os, requests

app = FastAPI(title="Guardião Risco Bot")

TELEGRAM_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "").strip()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Mensagem recebida no webhook:", data)

    # pega texto da mensagem ou channel_post
    msg = None
    if "message" in data and "text" in data["message"]:
        msg = data["message"]["text"]
    elif "channel_post" in data and "text" in data["channel_post"]:
        msg = data["channel_post"]["text"]

    if msg and TELEGRAM_TOKEN and TARGET_CHANNEL:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TARGET_CHANNEL, "text": msg}
        r = requests.post(url, json=payload, timeout=10)
        print("Repost status:", r.status_code, r.text)

    return {"ok": True}

@app.get("/send")
def send_message():
    if not TELEGRAM_TOKEN or not TARGET_CHANNEL:
        return {"error": "Faltam TG_BOT_TOKEN ou TARGET_CHANNEL no ambiente"}
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TARGET_CHANNEL, "text": "✅ Teste funcionando no canal! 🚀"}
    r = requests.post(url, json=payload, timeout=10)
    return {"status": r.status_code, "response": r.json()}
