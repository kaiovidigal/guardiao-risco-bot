#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webhook_app.py — forwarder simples p/ Telegram
- Rota: POST /webhook/{WEBHOOK_TOKEN}
- Lê updates (message/channel_post) e repassa para TARGET_CHANNEL
- Filtra por SOURCE_CHANNEL se definido
"""
import os
import asyncio
import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= ENV obrigatórias/opcionais =========
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()  # opcional

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not WEBHOOK_TOKEN:
    raise RuntimeError("Defina WEBHOOK_TOKEN no ambiente.")
if not TARGET_CHANNEL:
    raise RuntimeError("Defina TARGET_CHANNEL no ambiente (ex.: -1002796105884).")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI(title="tg-forwarder", version="1.0.0")

# ========= Helpers =========
async def tg(method: str, payload: dict):
    """Chamada simples para a API do Telegram."""
    url = f"{TELEGRAM_API}/{method}"
    timeout = httpx.Timeout(15.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload)
        try:
            data = r.json()
        except Exception:
            data = {"ok": False, "status_code": r.status_code, "text": r.text}
        if not data.get("ok"):
            print(f"[TG-ERR] {method} -> {data}")
        return data

def _extract_main(update: dict):
    """Extrai o 'conteúdo principal' do update do Telegram."""
    msg = (
        update.get("message")
        or update.get("channel_post")
        or update.get("edited_message")
        or update.get("edited_channel_post")
        or {}
    )
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    # texto/legenda
    text = msg.get("text") or msg.get("caption") or ""

    # mídia (só os principais para exemplo)
    photo = None
    if "photo" in msg and isinstance(msg["photo"], list) and msg["photo"]:
        # pega a maior resolução (último item)
        photo = msg["photo"][-1].get("file_id")

    audio_id = None
    if "audio" in msg and isinstance(msg["audio"], dict):
        audio_id = msg["audio"].get("file_id")

    voice_id = None
    if "voice" in msg and isinstance(msg["voice"], dict):
        voice_id = msg["voice"].get("file_id")

    document_id = None
    if "document" in msg and isinstance(msg["document"], dict):
        document_id = msg["document"].get("file_id")

    return chat_id, text, photo, audio_id, voice_id, document_id

async def forward_update(update: dict):
    """Repassa o conteúdo para o TARGET_CHANNEL (texto, foto+legenda, áudio, voice, documento)."""
    chat_id, text, photo, audio_id, voice_id, document_id = _extract_main(update)

    # filtro de origem (se definido)
    if SOURCE_CHANNEL and chat_id != str(SOURCE_CHANNEL):
        return {"ok": True, "skipped": "source_mismatch", "from": chat_id}

    if photo:
        return await tg("sendPhoto", {
            "chat_id": TARGET_CHANNEL,
            "photo": photo,
            "caption": text or ""
        })

    if audio_id:
        return await tg("sendAudio", {
            "chat_id": TARGET_CHANNEL,
            "audio": audio_id,
            "caption": text or ""
        })

    if voice_id:
        return await tg("sendVoice", {
            "chat_id": TARGET_CHANNEL,
            "voice": voice_id,
            "caption": text or ""
        })

    if document_id:
        return await tg("sendDocument", {
            "chat_id": TARGET_CHANNEL,
            "document": document_id,
            "caption": text or ""
        })

    # fallback: só texto
    if text:
        return await tg("sendMessage", {"chat_id": TARGET_CHANNEL, "text": text})

    # se não havia nada reconhecido, não falha — só loga
    print("[INFO] Update sem texto/mídia conhecida. Ignorado.")
    return {"ok": True, "ignored": True}

# ========= Rotas =========
@app.get("/")
async def root():
    return {"ok": True, "service": "tg-forwarder", "target": TARGET_CHANNEL, "source_filter": SOURCE_CHANNEL or None}

@app.get("/health")
async def health():
    # pequeno ping à API do Telegram para garantir reachability
    pong = await tg("getMe", {})
    return {"ok": True, "telegram_ok": pong.get("ok", False)}

@app.post(f"/webhook/{WEBHOOK_TOKEN}")
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    # o Telegram pode mandar lots de updates via webhook; processamos 1 a 1
    # (normalmente vem 1 por request)
    try:
        result = await forward_update(data)
    except Exception as e:
        print("[ERR] forward_update:", repr(e))
        # Nunca retornar 500 pro Telegram; responder 200 evita banir o webhook
        return {"ok": False, "error": str(e)}

    return {"ok": True, "forward_result": result}