import os, re, json, hmac, hashlib, time
from typing import Any, Dict
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import nest_asyncio
nest_asyncio.apply()

# ===== Config =====
CONF_MIN = float(os.getenv("CONF_MIN", "0.90"))
REQUIRE_G1 = os.getenv("REQUIRE_G1", "1") == "1"

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
DEST_CHAT_ID = int(os.getenv("DEST_CHAT_ID", "0"))

PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "@fantanvidigal")
AUTO_SCRAPE = os.getenv("AUTO_SCRAPE", "1") == "1"
SCRAPE_EVERY_S = int(os.getenv("SCRAPE_EVERY_S", "120"))
SCRAPE_LIMIT = int(os.getenv("SCRAPE_LIMIT", "1000"))

# ===== Bot (aiogram 2.x) =====
from aiogram import Bot, types
from aiogram.utils.exceptions import MessageNotModified

bot: Bot | None = None
if TG_BOT_TOKEN and DEST_CHAT_ID:
    bot = Bot(token=TG_BOT_TOKEN, parse_mode="HTML")

# ===== App =====
app = FastAPI(title="fantan-auto webhook")

# Cache simples pra evitar duplicados (hash->expiração)
CACHE: Dict[str, float] = {}
CACHE_TTL = 30 * 60  # 30 min

def _now() -> float:
    return time.time()

def cache_seen(key: str) -> bool:
    # purge
    now = _now()
    expired = [k for k,v in CACHE.items() if v < now]
    for k in expired:
        CACHE.pop(k, None)
    if key in CACHE:
        return True
    CACHE[key] = now + CACHE_TTL
    return False

# ===== Parsers =====
RE_PCT = re.compile(r"Acertamos\s+(\d{1,3}(?:[.,]\d+)?)%\s+das", re.I)
RE_SEQ = re.compile(r"Sequ[eê]ncia\s*:\s*([0-9]+)", re.I)
RE_MESA = re.compile(r"Mesa\s*:\s*([^\n\r]+)", re.I)
RE_ESTR = re.compile(r"Estrat[eé]gia\s*:\s*([0-9]+)", re.I)
RE_GALES = re.compile(r"Fazer até\s*([0-9]+)\s*gales", re.I)

def parse_signal(text: str) -> Dict[str, Any] | None:
    """Retorna um dict com os campos do sinal se for 'ENTRADA CONFIRMADA'."""
    if "ENTRADA CONFIRMADA" not in text.upper():
        return None

    pct = None
    m = RE_PCT.search(text)
    if m:
        pct = float(m.group(1).replace(",", "."))
        pct /= 100.0

    seq = None
    m = RE_SEQ.search(text)
    if m:
        seq = int(m.group(1))

    mesa = None
    m = RE_MESA.search(text)
    if m:
        mesa = m.group(1).strip()

    estrategia = None
    m = RE_ESTR.search(text)
    if m:
        estrategia = m.group(1)

    gales = None
    m = RE_GALES.search(text)
    if m:
        gales = int(m.group(1))

    return {
        "pct": pct,
        "seq": seq,
        "mesa": mesa,
        "estrategia": estrategia,
        "gales": gales,
        "raw": text.strip()
    }

def approved(sig: Dict[str, Any]) -> bool:
    # % mínima
    if sig["pct"] is not None and sig["pct"] < CONF_MIN:
        return False
    # G1 exigido
    if REQUIRE_G1 and (sig["seq"] is None or sig["seq"] != 1):
        return False
    return True

def fmt_signal(sig: Dict[str, Any]) -> str:
    pct_txt = f"{int(sig['pct']*100)}%" if sig["pct"] is not None else "N/D"
    seq_txt = f"G{sig['seq']}" if sig["seq"] is not None else "G?"
    mesa = sig["mesa"] or "-"
    estr = sig["estrategia"] or "-"
    gales = sig["gales"]
    gales_txt = f"{gales}" if gales is not None else "?"
    return (
        "<b>✅ SINAL CONFIRMADO</b>\n"
        f"• Mesa: <b>{mesa}</b>\n"
        f"• Estratégia: <code>{estr}</code>\n"
        f"• Sequência: <b>{seq_txt}</b>\n"
        f"• Confiança: <b>{pct_txt}</b>\n"
        f"• Gales até: <b>{gales_txt}</b>\n"
        "—\n"
        "<i>Origem:</i> canal público\n"
    )

# ===== Modelos =====
class TelegramUpdate(BaseModel):
    update_id: int | None = None
    message: Dict[str, Any] | None = None
    edited_message: Dict[str, Any] | None = None
    channel_post: Dict[str, Any] | None = None
    edited_channel_post: Dict[str, Any] | None = None

def extract_text(update: TelegramUpdate) -> tuple[str | None, int | None]:
    """Retorna (texto, message_id). Lê message / channel_post."""
    src = update.channel_post or update.message or update.edited_channel_post or update.edited_message
    if not src:
        return None, None
    txt = src.get("text") or src.get("caption")
    mid = src.get("message_id")
    return txt, mid

# ===== Endpoints =====
@app.get("/status")
async def status():
    return {
        "ok": True,
        "conf": {
            "CONF_MIN": CONF_MIN,
            "REQUIRE_G1": REQUIRE_G1,
        },
        "scrape": {
            "PUBLIC_CHANNEL": PUBLIC_CHANNEL,
            "SCRAPE_EVERY_S": SCRAPE_EVERY_S,
            "SCRAPE_LIMIT": SCRAPE_LIMIT,
            "AUTO_SCRAPE": 1 if AUTO_SCRAPE else 0,
        },
        "bot": {
            "enabled": 1 if bot else 0,
            "DEST_CHAT_ID": DEST_CHAT_ID,
        },
        "webhook": "/webhook/{TG_BOT_TOKEN}"
    }

@app.post("/webhook/{token}")
async def webhook_dyn(token: str, request: Request):
    # Opcional: recusar se token não confere com o do bot
    if TG_BOT_TOKEN and token != TG_BOT_TOKEN:
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=403)

    data = await request.json()
    upd = TelegramUpdate(**data)
    text, mid = extract_text(upd)

    if not text:
        return {"ok": True, "skipped": "no_text"}

    # Evitar duplicados por hash de conteúdo
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    if cache_seen(key):
        return {"ok": True, "skipped": "duplicate"}

    # Tenta parsear sinal
    sig = parse_signal(text)
    if not sig:
        return {"ok": True, "skipped": "no_signal"}

    if not approved(sig):
        return {"ok": True, "skipped": "filtered_out", "sig": sig}

    # Envia pro destino
    sent = False
    if bot and DEST_CHAT_ID:
        try:
            await bot.send_message(DEST_CHAT_ID, fmt_signal(sig))
            sent = True
        except Exception as e:
            print("Erro ao enviar ao bot:", e)

    return {"ok": True, "forwarded": sent, "sig": sig}

# ===== Início do serviço HTTP =====
if __name__ == "__main__":
    import uvicorn, os
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("webhook_app:app", host="0.0.0.0", port=port, reload=False)