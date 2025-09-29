# app.py
from fastapi import FastAPI, Request
from webhook_app import app  # importa o app principal

# --- Rotas auxiliares só para evitar 404/405 nos logs ---

@app.post("/webhook")
async def webhook_missing_token(request: Request):
    try:
        await request.body()  # consome o corpo
    except Exception:
        pass
    return {"ok": True, "hint": "Use /webhook/<WEBHOOK_TOKEN> com POST"}

@app.get("/webhook")
async def webhook_missing_token_get():
    return {"ok": True, "detail": "Este endpoint aceita POST. Para testar use /health."}

@app.get("/webhook/{token}")
async def webhook_get_info(token: str):
    return {"ok": True, "detail": "Webhook ativo. Envie POST pelo Telegram."}