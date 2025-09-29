# app.py
from webhook_app import app
from fastapi import Request

# Aceita POST errado em /webhook sem token (só limpa log)
@app.post("/webhook")
async def webhook_missing_token(request: Request):
    try:
        await request.body()
    except Exception:
        pass
    return {"ok": True, "hint": "Use /webhook/<WEBHOOK_TOKEN> (com POST)"}

# Aceita GET em /webhook (quando você abre no navegador)
@app.get("/webhook")
async def webhook_missing_token_get():
    return {"ok": True, "detail": "Este endpoint aceita POST. Para testar no navegador use /health."}

# Aceita GET em /webhook/{token} só pra evitar Method Not Allowed
@app.get("/webhook/{token}")
async def webhook_get_info(token: str):
    return {"ok": True, "detail": "Webhook ativo. Envie POST pelo Telegram."}