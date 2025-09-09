from __future__ import annotations

import os
import asyncio
import logging
from typing import Optional, List
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError  # <- nome correto
from telethon.tl.types import PeerChannel

# ──────────────────────────────────────────────────────────────────────────────
# Config / Logs
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("fantan-auto")

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()  # @usuario ou link

BASE_WEBHOOK_URL = os.getenv("BASE_WEBHOOK_URL", "").rstrip("/")

# thresholds / ritmo – com os valores “mais soltos” que combinamos
CONF_MIN = float(os.getenv("CONF_MIN", "0.82"))
MIN_SUP_G0 = int(os.getenv("MIN_SUP_G0", "10"))
MIN_SUP_G1 = int(os.getenv("MIN_SUP_G1", "8"))
N_MAX = int(os.getenv("N_MAX", "4"))
Z_WILSON = float(os.getenv("Z_WILSON", "1.96"))

COOLDOWN_S = int(os.getenv("COOLDOWN_S", "6"))
SCRAPE_EVERY_S = int(os.getenv("SCRAPE_EVERY_S", "120"))
SCRAPE_LIMIT = int(os.getenv("SCRAPE_LIMIT", "800"))
AUTO_SCRAPE = int(os.getenv("AUTO_SCRAPE", "1"))  # 1=liga o loop

# envio por bot (opcional)
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="guardiao-risco-bot webhook")

# ──────────────────────────────────────────────────────────────────────────────
# Telethon
# ──────────────────────────────────────────────────────────────────────────────
if not (API_ID and API_HASH and SESSION_STRING):
    log.error("API_ID/API_HASH/SESSION_STRING ausentes nas variáveis de ambiente.")
    # Não levantamos exceção aqui para permitir que a API suba e você veja /status

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
_connected_event = asyncio.Event()

# ──────────────────────────────────────────────────────────────────────────────
# Modelos Pydantic (v2)
# ──────────────────────────────────────────────────────────────────────────────
class IngestPayload(BaseModel):
    text: str = Field(..., description="Texto bruto para análise/ingestão")
    message_id: Optional[int] = None
    date: Optional[datetime] = None


class ScrapeRequest(BaseModel):
    limit: int = Field(SCRAPE_LIMIT, ge=1, le=5000)
    from_date: Optional[datetime] = None  # UTC
    to_date: Optional[datetime] = None    # UTC


# ──────────────────────────────────────────────────────────────────────────────
# Utilidades / “Core” bem simples (placeholder do seu analisador)
# ──────────────────────────────────────────────────────────────────────────────
_last_signal_ts = 0.0

def _passes_cooldown(now_ts: float) -> bool:
    global _last_signal_ts
    if now_ts - _last_signal_ts >= COOLDOWN_S:
        _last_signal_ts = now_ts
        return True
    return False

def analyze_text_to_signal(text: str) -> Optional[str]:
    """
    Placeholder simples: você pode ligar aqui seu classificador real.
    Usa os thresholds definidos (CONF_MIN etc) se quiser.
    """
    t = text.lower()
    # Exemplo bobo de “disparo” – troque pelo seu modelo
    if "entrada" in t or "sinal" in t or "fan tan" in t:
        return f"✅ SINAL (simulado)\nconf>={CONF_MIN:.2f} | N<={N_MAX} | g0>={MIN_SUP_G0} g1>={MIN_SUP_G1}\n\n{text}"
    return None

async def send_via_bot(message: str) -> None:
    """
    Envia o texto para o chat do bot, se TG_BOT_TOKEN e TG_CHAT_ID estiverem setados.
    Para evitar dependência do aiogram, usamos curl do Telegram HTTP API via aiohttp opcional.
    """
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return
    import aiohttp
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, timeout=30) as r:
            if r.status != 200:
                txt = await r.text()
                log.warning("Falha ao enviar via bot (%s): %s", r.status, txt)

# ──────────────────────────────────────────────────────────────────────────────
# Rotas
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"ok": True, "service": "guardiao-risco-bot", "connected": _connected_event.is_set()}

@app.get("/status")
async def status():
    return {
        "ok": True,
        "conf": {
            "CONF_MIN": CONF_MIN, "MIN_SUP_G0": MIN_SUP_G0, "MIN_SUP_G1": MIN_SUP_G1,
            "N_MAX": N_MAX, "Z_WILSON": Z_WILSON,
            "COOLDOWN_S": COOLDOWN_S,
        },
        "scrape": {
            "PUBLIC_CHANNEL": PUBLIC_CHANNEL,
            "SCRAPE_EVERY_S": SCRAPE_EVERY_S,
            "SCRAPE_LIMIT": SCRAPE_LIMIT,
            "AUTO_SCRAPE": AUTO_SCRAPE,
        },
        "bot": {"enabled": bool(TG_BOT_TOKEN and TG_CHAT_ID)},
        "webhook": BASE_WEBHOOK_URL + "/webhook",
    }

@app.post("/ingest")
async def ingest(payload: IngestPayload):
    """
    Recebe texto e tenta gerar sinal.
    """
    sig = analyze_text_to_signal(payload.text)
    if sig:
        now_ts = datetime.now(tz=timezone.utc).timestamp()
        if _passes_cooldown(now_ts):
            await send_via_bot(sig)
            log.info("SINAL enviado (ingest).")
            return {"ok": True, "sent": True, "reason": "cooldown passou"}
        else:
            log.info("SINAL gerado mas bloqueado por cooldown.")
            return {"ok": True, "sent": False, "reason": "cooldown"}
    return {"ok": True, "sent": False, "reason": "sem_padroes"}

@app.post("/scrape")
async def scrape(req: ScrapeRequest, bt: BackgroundTasks):
    """
    Dispara uma raspagem única do histórico.
    """
    if not PUBLIC_CHANNEL:
        raise HTTPException(400, "PUBLIC_CHANNEL ausente.")

    async def _job():
        await ensure_connected()
        await scrape_once(limit=req.limit, from_date=req.from_date, to_date=req.to_date)

    bt.add_task(_job)
    return {"ok": True, "queued": True}

# Um endpoint simples de webhook “placeholder” (caso queira plugar bot webhook no futuro)
@app.post("/webhook")
async def webhook_stub(update: dict):
    log.info("Webhook recebido: %s", str(update)[:300])
    return {"ok": True}

# ──────────────────────────────────────────────────────────────────────────────
# Scraper
# ──────────────────────────────────────────────────────────────────────────────
async def ensure_connected():
    if _connected_event.is_set():
        return
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Session inválida: não autorizado")
        _connected_event.set()
        log.info("📬 Retroativo conectado no Telegram com Telethon")
    except Exception as e:
        log.exception("Falha conectando ao Telegram: %s", e)
        raise

async def iter_messages_safe(entity, limit: int):
    """
    Iterador resiliente com tratamento de FloodWait.
    """
    fetched = 0
    async for msg in client.iter_messages(entity, limit=limit):
        yield msg
        fetched += 1
        if fetched % 200 == 0:
            await asyncio.sleep(1)

async def scrape_once(limit: int, from_date: Optional[datetime], to_date: Optional[datetime]):
    await ensure_connected()
    entity = PUBLIC_CHANNEL
    try:
        # Resolve link/@ para Peer
        entity = await client.get_entity(entity)  # PeerChannel/Channel
    except RPCError as e:
        log.error("Erro resolvendo canal (%s): %s", PUBLIC_CHANNEL, e)
        return

    processed = 0
    async for m in iter_messages_safe(entity, limit=limit):
        # Filtra por data, se informado
        if from_date and m.date and m.date.replace(tzinfo=timezone.utc) < from_date.replace(tzinfo=timezone.utc):
            continue
        if to_date and m.date and m.date.replace(tzinfo=timezone.utc) > to_date.replace(tzinfo=timezone.utc):
            continue

        if not m.message:
            continue

        sig = analyze_text_to_signal(m.message)
        if sig:
            now_ts = datetime.now(tz=timezone.utc).timestamp()
            if _passes_cooldown(now_ts):
                await send_via_bot(sig)
        processed += 1

    log.info("Scrape concluído. Mensagens processadas: %s", processed)

async def scrape_loop():
    """
    Loop automático de raspagem, se AUTO_SCRAPE=1.
    """
    if not AUTO_SCRAPE:
        log.info("Loop de scrape automático desativado (AUTO_SCRAPE=0).")
        return
    if not PUBLIC_CHANNEL:
        log.warning("PUBLIC_CHANNEL vazio; loop de scrape não iniciará.")
        return

    await ensure_connected()
    log.info("⏳ Loop de scrape iniciado (cada %ss, limite=%s).", SCRAPE_EVERY_S, SCRAPE_LIMIT)

    while True:
        try:
            await scrape_once(limit=SCRAPE_LIMIT, from_date=None, to_date=None)
        except FloodWaitError as e:
            log.warning("FloodWait: aguardando %ss", int(e.seconds) + 1)
            await asyncio.sleep(int(e.seconds) + 1)
        except Exception as e:
            log.exception("Erro no loop de scrape: %s", e)
            await asyncio.sleep(5)
        await asyncio.sleep(SCRAPE_EVERY_S)

# ──────────────────────────────────────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    # Conecta e registra logs
    try:
        await ensure_connected()
        if BASE_WEBHOOK_URL:
            log.info("Webhook registrado em %s/webhook", BASE_WEBHOOK_URL)
    finally:
        # inicia loop de scrape em background
        asyncio.create_task(scrape_loop())
        log.info("INFORMAÇÕES: Inicialização do aplicativo concluída.")