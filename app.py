from fastapi import FastAPI, Request

app = FastAPI()

WEBHOOK_SECRET = "meusegredo123"  # mesmo usado no setWebhook

@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        print("Update recebido:", data)
        # TODO: sua lógica de resposta ao Telegram aqui
        return {"ok": True}
    except Exception as e:
        print("Erro no webhook:", str(e))
        return {"ok": False, "error": str(e)}