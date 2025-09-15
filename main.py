from fastapi import FastAPI, Request
import requests

app = FastAPI()

# Token do seu bot
TELEGRAM_TOKEN = "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4"

# ID do canal de destino (seu canal)
DEST_CHAT_ID = "-1003052132833"

# ID do canal fonte (que você não quer repassar direto)
SOURCE_CHAT_ID = "-1002810508717"

# --- ROTA DE WEBHOOK ---
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Mensagem recebida:", data)  # só debug

    # Verifica se veio do canal fonte
    if "channel_post" in data:
        chat_id = str(data["channel_post"]["chat"]["id"])
        text = data["channel_post"].get("text", "")

        if chat_id == SOURCE_CHAT_ID:
            print("Mensagem do canal fonte ignorada.")
            return {"ok": True}

        # Aqui você poderia aplicar regras e depois mandar pro destino
        send_message("🚀 Novo sinal processado!")
    
    return {"ok": True}

# --- FUNÇÃO DE ENVIO ---
def send_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": DEST_CHAT_ID,
        "text": text
    }
    r = requests.post(url, json=payload)
    print("Resposta envio:", r.json())
    return r.json()

# --- ROTA DE TESTE ---
@app.get("/send")
async def send_test():
    return send_message("✅ Teste funcionando no canal de destino!")