from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import requests
import os

app = FastAPI(title="Guardião Risco Bot")

# Pegue de variáveis de ambiente (recomendado no Render)
# ou caia para os defaults que você me passou.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8217345207:AAEf5DjyRgIzxtDlTZVJX5bOjLw-uSg_i5o")
DEFAULT_CHAT_ID = os.getenv("CHAT_ID", "-1001234567890")  # troque pelo ID real do canal/grupo

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def send_to_telegram(chat_id: str, text: str):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=10)
    try:
        j = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f"Telegram retornou {r.status_code}: {r.text}")
    if not j.get("ok"):
        raise HTTPException(status_code=502, detail=j)
    return j

# ---------- WEBHOOK DO TELEGRAM (apenas log) ----------
@app.post("/webhook/" + TELEGRAM_TOKEN)
async def telegram_webhook(request: Request):
    data = await request.json()
    print("Webhook Telegram recebido:", data)
    # aqui você poderia reagir a comandos (/start, etc), se quiser
    return {"ok": True}

# ---------- TESTE RÁPIDO ----------
@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/send")
def send_test():
    return send_to_telegram(DEFAULT_CHAT_ID, "✅ Teste funcionando! 🚀")

# ---------- ENVIAR SINAIS ----------
class SignalIn(BaseModel):
    text: str                      # texto completo do sinal (ex: "ENTRADA CONFIRMADA...\nSequência: 1 | 4 | 2 ...")
    chat_id: str | None = None     # opcional: sobrescrever o chat destino

@app.post("/signal")
def send_signal(sinal: SignalIn):
    chat_id = sinal.chat_id or DEFAULT_CHAT_ID
    text = sinal.text.strip()

    # Opcional: pequenas melhorias de formatação pra ficar bonito
    # Exemplo: destacar "ENTRADA CONFIRMADA" se existir
    if text.upper().startswith("ENTRADA"):
        text = f"✅ <b>{text.splitlines()[0].strip()}</b>\n" + "\n".join(text.splitlines()[1:])

    return send_to_telegram(chat_id, text)