# main.py
from fastapi import FastAPI, Request
import requests
from api_fanta import get_latest_result  # mantém a rota /
import os

app = FastAPI(title="Guardião Risco Bot")

# ==== CONFIG TELEGRAM ====
TELEGRAM_TOKEN = "8217345207:AAEf5DjyRgIzxtDlTZVJX5bOjLw-uSg_i5o"
CHAT_ID = "-1003052132833"  # seu canal/grupo

# Healthcheck
@app.get("/health")
async def health():
    return {"ok": True}

# Raiz: continua usando seu leitor da API (pode ficar como está)
@app.get("/")
async def root():
    result = await get_latest_result()
    if result:
        numero, ts_epoch = result
        return {"numero": numero, "timestamp": ts_epoch}
    return {"erro": "Nenhum resultado válido encontrado"}

# ---- ROTAS DO TELEGRAM ----
# GET para testar no navegador (mostra que a rota existe)
@app.get(f"/webhook/{TELEGRAM_TOKEN}")
async def webhook_get():
    return {"ok": True, "msg": "Webhook do Telegram ativo (método GET)."}

# POST: o Telegram (ou seu teste via curl) envia os sinais aqui
@app.post(f"/webhook/{TELEGRAM_TOKEN}")
async def webhook_post(request: Request):
    data = await request.json()
    # log simples no console
    print("Mensagem recebida no webhook:", data)
    return {"ok": True}

# Envio manual de mensagem para testar
@app.get("/send")
async def send_message():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": "✅ Teste funcionando! 🚀"
    }
    r = requests.post(url, json=payload, timeout=15)
    try:
        return {"status": r.json()}
    except Exception:
        return {"status_code": r.status_code, "text": r.text}