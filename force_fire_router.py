# -*- coding: utf-8 -*-
# force_fire_router.py — rota manual de disparo de FIRE no Fantan Guardião

import os
import httpx
from fastapi import APIRouter, Query

# ENV
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
REPL_CHANNEL   = os.getenv("REPL_CHANNEL", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
FLUSH_KEY      = os.getenv("FLUSH_KEY", "meusegredo123").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

router = APIRouter()


async def _send_message(chat_id: str, text: str):
    """Função auxiliar para enviar mensagem ao Telegram."""
    if not TG_BOT_TOKEN or not chat_id:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
    return True


@router.get("/debug/force_fire")
async def debug_force_fire(
    key: str = Query(default=""),
    number: int = Query(default=1),
    pattern: str = Query(default="ODD"),
    base: str = Query(default="[1,3]"),
    conf: float = Query(default=0.72),
    samples: int = Query(default=452122),
):
    """Disparo manual de FIRE (teste/debug)."""
    if not key or key != FLUSH_KEY:
        return {"ok": False, "error": "unauthorized"}

    txt = (
        f"🤖 <b>Tiro seco por IA [FIRE]</b>\n"
        f"🎯 Número seco (G0): <b>{number}</b>\n"
        f"🧩 Padrão: {pattern}\n"
        f"🧮 Base: {base}\n"
        f"📊 Conf: {conf*100:.2f}% | Amostra≈{samples}"
    )

    channel = REPL_CHANNEL or PUBLIC_CHANNEL
    await _send_message(channel, txt)

    return {"ok": True, "fire": number, "conf": conf, "samples": samples}


def attach_force_fire(app):
    """Anexa este router à instância principal do FastAPI."""
    app.include_router(router)