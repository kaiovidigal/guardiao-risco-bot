from fastapi import FastAPI, Request
import os, requests

app = FastAPI(title="Guardião Risco Bot")

TELEGRAM_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

@app.get("/health")
def health():
    return {"ok": True}

# Webhook para receber sinais (POST em /webhook)
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    # aqui você pode transformar 'data' em uma mensagem formatada
    print("Mensagem recebida no webhook:", data)
    return {"ok": True}

# Rota de teste para enviar mensagem ao Telegram
@app.get("/send")
def send_message():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return {"error": "Faltam TG_BOT_TOKEN ou CHAT_ID no ambiente"}
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": "✅ Teste funcionando no canal! 🚀"}
    r = requests.post(url, json=payload, timeout=10)
    return {"status": r.status_code, "response": r.json()}