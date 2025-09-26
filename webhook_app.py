#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webhook_app.py — mínimo e funcional
- Expõe rota POST /webhook/{WEBHOOK_TOKEN}
- Valida token do caminho com a ENV WEBHOOK_TOKEN
- Responde 200 rapidamente (útil p/ Telegram)
- Endpoints auxiliares: "/" e "/health"

ENVs necessárias:
  - TG_BOT_TOKEN   (string; não é usado aqui, mas deixei para compatibilidade)
  - WEBHOOK_TOKEN  (ex.: "meusegredo123")
"""

import os
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

# ===== ENVs =====
TG_BOT_TOKEN  = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()

if not WEBHOOK_TOKEN:
    raise RuntimeError("Defina WEBHOOK_TOKEN no ambiente.")

app = FastAPI(title="Webhook mínimo (Telegram)", version="1.0.0")


# ===== Raiz =====
@app.get("/")
async def root():
    return {"ok": True, "service": "webhook-min", "version": "1.0.0"}


# ===== Health =====
@app.get("/health")
async def health():
    return {"ok": True, "has_token": bool(WEBHOOK_TOKEN), "token_len": len(WEBHOOK_TOKEN)}


# ===== Webhook do Telegram (POST APENAS) =====
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    # 1) valida token do path
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    # 2) tenta ler JSON (Telegram envia JSON)
    try:
        data = await request.json()
    except Exception:
        # se não for JSON, tenta ler texto bruto só para não quebrar
        body = await request.body()
        try:
            data = json.loads(body.decode("utf-8", errors="ignore"))
        except Exception:
            data = {"raw": body.decode("utf-8", errors="ignore")}

    # 3) (opcional) log simples no stdout do Render
    print(">> webhook update:", json.dumps(data, ensure_ascii=False)[:2000], flush=True)

    # 4) responde OK para o Telegram
    return JSONResponse({"ok": True})


# ==== Observação de teste ====
# Se você abrir GET /webhook/{WEBHOOK_TOKEN} no navegador,
# o FastAPI responderá "405 Method Not Allowed".
# Isso é bom: significa que a ROTA EXISTE (mas só aceita POST).
#
# Ex.: https://SEU-APP.onrender.com/webhook/meusegredo123  -> 405
#
# No Telegram, use:
# https://api.telegram.org/bot<SEU_TOKEN>/setWebhook?url=https://SEU-APP.onrender.com/webhook/meusegredo123
#
# Se o getWebhookInfo disser "404 Not Found", é porque a rota não bate com o caminho acima.