from fastapi import FastAPI, Request
import os, requests
from api_fanta import get_latest_result  # mantém sua rota "/" do Fan-Tan

app = FastAPI(title="Guardião Risco Bot")

TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
DEST_CHAT_ID   = os.getenv("DEST_CHAT_ID", "").strip()      # canal destino
SOURCE_CHAT_ID = int(os.getenv("SOURCE_CHAT_ID", "0"))      # canal fonte
FORWARD_FROM_SOURCE = os.getenv("FORWARD_FROM_SOURCE", "0") == "1"

TELEGRAM_SEND_URL = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

def send_to_dest(text: str):
    if not TG_BOT_TOKEN or not DEST_CHAT_ID:
        return {"ok": False, "reason": "missing env"}
    r = requests.post(TELEGRAM_SEND_URL, json={
        "chat_id": DEST_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }, timeout=10)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "status": r.status_code, "text": r.text}

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/")
async def root():
    result = await get_latest_result()
    if result:
        numero, ts_epoch = result
        return {"numero": numero, "timestamp": ts_epoch}
    return {"erro": "Nenhum resultado válido encontrado"}

# --------- WEBHOOK DO TELEGRAM (recebe atualizações) ----------
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Mensagem recebida:", data)

    # Só interessa quando vier uma postagem de canal
    channel_post = data.get("channel_post")
    if not channel_post:
        return {"ok": True, "ignored": "not_channel_post"}

    chat = channel_post.get("chat", {})
    chat_id = chat.get("id")

    # TESTE/Desbloqueio: se habilitado, repassa mensagens do canal-fonte
    if FORWARD_FROM_SOURCE and chat_id == SOURCE_CHAT_ID:
        text = channel_post.get("text") or channel_post.get("caption") or "(sem texto)"
        res = send_to_dest(f"🔓 REPASSE TESTE\n\n{text}")
        print("Repassado (teste):", res)
        return {"ok": True, "forwarded": True, "res": res}

    # Com FORWARD_FROM_SOURCE desligado, tudo que vier do fonte será ignorado
    return {"ok": True, "ignored": True}

# --------- ROTA DE TESTE MANUAL ----------
@app.get("/send")
async def send_message():
    res = send_to_dest("✅ Teste funcionando no canal de destino! 🚀")
    return {"status": res}