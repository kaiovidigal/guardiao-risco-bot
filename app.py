# app.py (opcional, só pra não quebrar nada)
from webhook_app import app  # noqa: F401


# --- Rotas auxiliares para eliminar 404/405 sem tocar na lógica principal ---

@app.post("/webhook")
async def webhook_missing_token(request: Request):
    # Retorna 200 para chamadas erradas sem token, só para não poluir os logs
    try:
        _ = await request.body()  # consome o corpo
    except Exception:
        pass
    return {"ok": True, "hint": "Use /webhook/<WEBHOOK_TOKEN> (com POST)"}

@app.get("/webhook")
async def webhook_missing_token_get():
    # Navegador/GET: mostra explicação amigável
    return {"ok": True, "detail": "Este endpoint aceita POST. Para testar no navegador use /health."}

@app.get("/webhook/{token}")
async def webhook_get_info(token: str):
    # Evita 'Method Not Allowed' quando você abre o link no navegador
    return {"ok": True, "detail": "Webhook ativo. Envie POST pelo Telegram."}