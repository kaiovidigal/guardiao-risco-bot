from fastapi import FastAPI, Request
import requests

app = FastAPI(title="Guardião Risco Bot")

# Seu token do bot
TELEGRAM_TOKEN = "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4"

# Canal de destino
DESTINO_ID = -1003052132833  # @fantanautomatico2

# Canal de origem (fonte)
FONTE_ID = -1002810508717  # @fantanvidigal


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Mensagem recebida:", data)

    chan = data.get("channel_post") or data.get("edited_channel_post")
    if not chan:
        return {"ok": True, "info": "ignorado: não é channel_post"}

    chat = chan.get("chat", {})
    chat_id = chat.get("id")
    text = chan.get("text") or chan.get("caption") or ""

    # Só repassa se veio do canal fonte
    if chat_id != FONTE_ID:
        return {"ok": True, "info": f"ignorado: chat {chat_id} != fonte"}

    # Envia para o canal de destino
    payload = {
        "chat_id": DESTINO_ID,
        "text": text or "Mensagem sem texto",
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json=payload,
        timeout=10,
    )
    print("Resultado do envio:", r.text)
    return {"ok": True}