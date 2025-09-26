#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forwarder_webhook.py — v1.0.0
- Lê do SOURCE_CHANNEL e repassa para TARGET_CHANNEL
- Apenas nas janelas horárias: 02:00, 10:00, 15:00, 19:00 (1h cada)
- Copia a mensagem original (texto/mídia) com Telegram copyMessage
- Webhook: POST /webhook/{WEBHOOK_TOKEN}

ENV obrigatórias:
  TG_BOT_TOKEN, WEBHOOK_TOKEN

ENV opcionais:
  SOURCE_CHANNEL    (default: -1003052132833)
  TARGET_CHANNEL    (default: -1002796105884)
  TZ_NAME           (default: America/Sao_Paulo)
  DEBUG_MSG         (1/0; default: 0)
  ALLOWED_SLOTS     (HH:MM separados por vírgula; default: 02:00,10:00,15:00,19:00)
  SLOT_DURATION_MIN (minutos da janela; default: 60)
"""
import os, json, time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= ENV / Const =========
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "-1003052132833").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "-1002796105884").strip()
TZ_NAME        = os.getenv("TZ_NAME", "America/Sao_Paulo").strip()
DEBUG_MSG      = os.getenv("DEBUG_MSG", "0").strip() in ("1","true","True","yes","YES")

ALLOWED_SLOTS  = [s.strip() for s in os.getenv("ALLOWED_SLOTS", "02:00,10:00,15:00,19:00").split(",") if s.strip()]
SLOT_DURATION  = int(os.getenv("SLOT_DURATION_MIN", "60"))

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not WEBHOOK_TOKEN:
    raise RuntimeError("Defina WEBHOOK_TOKEN no ambiente.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ========= Timezone =========
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(TZ_NAME)
except Exception:
    _TZ = timezone.utc

def tz_now():
    return datetime.now(_TZ)

def in_allowed_window(dt: datetime) -> bool:
    """Retorna True se dt estiver dentro de qualquer janela (slot) configurada."""
    # Normaliza para o dia atual no fuso dado
    base = dt.replace(second=0, microsecond=0)
    for slot in ALLOWED_SLOTS:
        try:
            hh, mm = slot.split(":")
            slot_start = base.replace(hour=int(hh), minute=int(mm))
        except Exception:
            continue
        # se já passou do slot neste dia, ok; se ainda não chegou, considera slot do mesmo dia
        # janela padrão: [start, start + SLOT_DURATION)
        slot_end = slot_start + timedelta(minutes=SLOT_DURATION)
        if slot_start <= base < slot_end:
            return True
    return False

# ========= App =========
app = FastAPI(title="simple-forwarder-webhook", version="1.0.0")

async def tg_copy_message(from_chat_id: str, to_chat_id: str, message_id: int):
    """Usa copyMessage para preservar conteúdo/legenda/formatação da mensagem."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{TELEGRAM_API}/copyMessage", json={
                "chat_id": to_chat_id,
                "from_chat_id": from_chat_id,
                "message_id": message_id,
                # opcional: desabilitar prévia de links no destino
                "disable_notification": False,
                "protect_content": False
            })
            return r.json()
    except Exception as e:
        if DEBUG_MSG:
            print(f"[copyMessage ERR] {e}")
        return {"ok": False, "error": str(e)}

async def tg_send_text(chat_id: str, text: str):
    if not DEBUG_MSG:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{TELEGRAM_API}/sendMessage",
                              json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                    "disable_web_page_preview": True})
    except Exception:
        pass

def _extract_message(payload: dict):
    """Extrai o objeto de mensagem do update (canal ou chat)."""
    # Prioriza channel_post e edited_channel_post (canais)
    msg = payload.get("channel_post") or payload.get("edited_channel_post")
    if not msg:
        msg = payload.get("message") or payload.get("edited_message")
    return msg or {}

@app.get("/")
async def root():
    return {"ok": True, "service": "simple-forwarder-webhook", "time": tz_now().isoformat()}

@app.get("/health")
async def health():
    return {
        "ok": True,
        "tz": TZ_NAME,
        "slots": ALLOWED_SLOTS,
        "slot_minutes": SLOT_DURATION,
        "time": tz_now().isoformat(),
        "source": SOURCE_CHANNEL,
        "target": TARGET_CHANNEL
    }

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    msg = _extract_message(data)
    if not msg:
        return {"ok": True, "skipped": "no_message"}

    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    message_id = msg.get("message_id")

    # Filtra por canal de origem
    if chat_id != str(SOURCE_CHANNEL):
        if DEBUG_MSG:
            await tg_send_text(TARGET_CHANNEL, f"DEBUG: ignorando chat {chat_id}, esperado {SOURCE_CHANNEL}")
        return {"ok": True, "skipped": "wrong_source"}

    # Aplica janela horária
    now = tz_now()
    if not in_allowed_window(now):
        if DEBUG_MSG:
            await tg_send_text(TARGET_CHANNEL, "DEBUG: fora da janela — não repassado.")
        return {"ok": True, "skipped": "outside_allowed_window", "now": now.isoformat()}

    # Copia a mensagem original
    if not isinstance(message_id, int):
        if DEBUG_MSG:
            await tg_send_text(TARGET_CHANNEL, "DEBUG: message_id inválido, não repassado.")
        return {"ok": True, "skipped": "no_message_id"}

    result = await tg_copy_message(SOURCE_CHANNEL, TARGET_CHANNEL, message_id)
    return {"ok": True, "forwarded": bool(result.get("ok")), "tg_result": result}