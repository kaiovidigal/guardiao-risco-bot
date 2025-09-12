# force_fire_router.py
# Add-on para expor /debug/force_fire como GET e enviar FIRE imediato no canal

import os
from fastapi import Query
from fastapi import APIRouter
import httpx
import json

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
REPL_CHANNEL = os.getenv("REPL_CHANNEL", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
FLUSH_KEY    = os.getenv("FLUSH_KEY", "meusegredo123").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

router = APIRouter()

async def _send_message(chat_id: str, text: str):
    if not TG_BOT_TOKEN or not chat_id:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True})
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
    if not key or key != FLUSH_KEY:
        return {"ok": False, "error": "unauthorized"}

    txt = (f"ð¤ <b>Tiro seco por IA [FIRE]</b>\n"
           f"ð¯ NÃºmero seco (G0): <b>{number}</b>\n"
           f"ð§© PadrÃ£o: {pattern}\n"
           f"ð§® Base: {base}\n"
           f"ð Conf: {conf*100:.2f}% | Amostraâ{samples}")

    channel = REPL_CHANNEL or PUBLIC_CHANNEL
    await _send_message(channel, txt)

    return {"ok": True, "fire": number, "conf": conf, "samples": samples}

def attach_force_fire(app):
    app.include_router(router)
