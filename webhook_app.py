# webhook_app.py — versão corrigida e mínima para subir no Render/GitHub
# FastAPI + envio ao Telegram (IA FIRE de exemplo) — sem erros de f-string

import os
import httpx
from fastapi import FastAPI, Request
from typing import Optional

# =====================
# ENV obrigatórias
# =====================
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()  # -100... ou @canal
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI(title="Guardião IA", version="1.0.1")


# =====================
# Helpers Telegram
# =====================
async def tg_send_text(chat_id: str, text: str, parse: str = "HTML") -> None:
    """Envia texto para o Telegram com httpx (async)."""
    if not TG_BOT_TOKEN or not chat_id:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse,
                "disable_web_page_preview": True,
            },
        )


async def ia_fire_example(best: int, conf: float) -> None:
    """Exemplo de envio de sinal de IA (para validar integração)."""
    # ⚠️ ESTA LINHA É A QUE QUEBROU ANTES — AGORA ESTÁ CORRETA
    await tg_send_text(
        PUBLIC_CHANNEL,
        f"🤖 IA FIRE — Número: {best} | Confiança: {conf*100:.2f}%"
    )


# =====================
# Rotas
# =====================
@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN> ou GET /debug/ping"}


@app.get("/debug/ping")
async def debug_ping():
    """Ping simples para checar se o app está vivo no Render."""
    return {"ok": True, "pong": True}


@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    """Webhook do Telegram. Para testar: envie '/teste' para o bot."""
    if token != WEBHOOK_TOKEN:
        return {"ok": False, "error": "token_invalido"}

    try:
        data = await request.json()
    except Exception:
        data = {}

    message = data.get("message") or data.get("channel_post") or {}
    text: str = (message.get("text") or "").strip()

    # Comando de teste para validar envio:
    if text == "/teste":
        await ia_fire_example(best=2, conf=0.74)
        return {"ok": True, "sent": True}

    # Aqui você pode plugar sua lógica real de IA/análise:
    # - Parse de mensagens do canal (GREEN/RED/ANALISANDO)
    # - Atualização de bancos, n-grams, etc.
    # - Decisão de FIRE e uso de tg_send_text(...)
    return {"ok": True, "skipped": True}


# =====================
# Observações de Deploy
# =====================
# Procfile (use uma das linhas abaixo):
#   web: uvicorn webhook_app:app --host 0.0.0.0 --port $PORT
# ou
#   web: gunicorn webhook_app:app --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
#
# requirements.txt mínimos:
#   fastapi==0.111.0
#   pydantic==2.8.2
#   httpx==0.27.0
#   uvicorn[standard]==0.30.1
