# app.py (arquivo de entrada principal do Render/uvicorn)
from webhook_app import app  # importa o FastAPI definido no webhook_app.py

from fastapi import Request

# --- Rotas auxiliares para eliminar 404/405 sem quebrar lógica principal ---

@app.post("/webhook")
async def webhook_missing_token(request: Request):
    # Retorna 200 para chamadas erradas sem token (ex.: /webhook sem /meusegredo123)
    try:
        _ = await request.body()  # consome o corpo pra não dar erro de stream
    except Exception:
        pass
    return {"ok": True, "hint": "Use /webhook/<WEBHOOK_TOKEN> (com POST)"}

@app.get("/webhook")
async def webhook_missing_token_get():
    # Se alguém abrir no navegador (GET), mostra explicação amigável
    return {"ok": True, "detail": "Este endpoint aceita POST. Para testar no navegador use /health."}

@app.get("/webhook/{token}")
async def webhook_get_info(token: str):
    # Evita "Method Not Allowed" quando você abre no navegador
    return {"ok": True, "detail": f"Webhook ativo para token={token}. Envie POST pelo Telegram."}