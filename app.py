# app.py
from webhook_app import app
from fastapi import Request

@app.post("/webhook")
async def webhook_missing_token(request: Request):
    try: _ = await request.body()
    except Exception: pass
    return {"ok": True, "hint": "Use /webhook/<WEBHOOK_TOKEN> (com POST)"}

@app.get("/webhook")
async def webhook_missing_token_get():
    return {"ok": True, "detail": "Este endpoint aceita POST. Para testar no navegador use /health."}

@app.get("/webhook/{token}")
async def webhook_get_info(token: str):
    return {"ok": True, "detail": f"Webhook ativo para token={token}. Envie POST pelo Telegram."}