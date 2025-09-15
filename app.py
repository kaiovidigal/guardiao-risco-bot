from fastapi import FastAPI, Request
import requests

TELEGRAM_TOKEN = "8217345207:AAEf5DjyRgIzxtDlTZVJX5bOjLw-uSg_i5o"
CHAT_ID = "-1003052132833"  # seu canal/grupo

app = FastAPI(title="Ping de teste")

@app.get("/")
async def root():
    return {"ok": True, "tip": "abra /send para mandar msg de teste"}

@app.get("/health")
async def health():
    return {"ok": True}

# Teste de envio de mensagem
@app.get("/send")
async def send_message():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": "✅ Teste funcionando! 🚀"}
    r = requests.post(url, json=payload, timeout=10)
    return r.json()

# (opcional) Webhook só para ver se chega algo
@app.post(f"/webhook/{TELEGRAM_TOKEN}")
async def webhook(request: Request):
    data = await request.json()
    print("Mensagem recebida no webhook:", data)
    return {"ok": True}