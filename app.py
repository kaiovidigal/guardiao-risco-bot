from fastapi import FastAPI, Request
import requests

app = FastAPI(title="Guardião Risco Bot")

# === CONFIG (pode hardcode por enquanto) ===
TELEGRAM_TOKEN = "8217345207:AAEf5DjyRgIzxtDlTZVJX5bOjLw-uSg_i5o"
CHAT_ID = "-1003052132833"  # id do seu canal/grupo

# --- rotas básicas para não dar 404 ---
@app.get("/")
async def root():
    return {"ok": True, "msg": "Guardião Risco Bot ativo"}

@app.get("/health")
async def health():
    return {"ok": True}

# --- rota de teste: envia mensagem no Telegram ---
@app.get("/send")
async def send_message():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": "✅ Teste funcionando! 🚀"}
    r = requests.post(url, json=payload, timeout=10)
    try:
        return {"status": r.status_code, "resp": r.json()}
    except Exception:
        return {"status": r.status_code, "resp_text": r.text}

# --- webhook para receber sinais (POST) ---
@app.post("/webhook/" + TELEGRAM_TOKEN)
async def webhook(request: Request):
    data = await request.json()
    print("Mensagem recebida:", data)
    # se quiser já encaminhar para o Telegram, descomente abaixo:
    # text = data.get("text") or "📩 Webhook recebido!"
    # requests.post(
    #     f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
    #     json={"chat_id": CHAT_ID, "text": text},
    #     timeout=10
    # )
    return {"ok": True}