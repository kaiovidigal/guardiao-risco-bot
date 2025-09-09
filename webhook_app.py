import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RpcError
from telethon.tl.types import Message

# ----------------------------
# Config / Env
# ----------------------------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

PUBLIC_CHANNEL = os.environ.get("PUBLIC_CHANNEL", "")  # ex: @fantanvidigal
SCRAPE_LIMIT = int(os.environ.get("SCRAPE_LIMIT", "500"))
SCRAPE_EVERY_S = int(os.environ.get("SCRAPE_EVERY_S", "120"))

# Parâmetros de sinais (defaults “mais soltos”)
CONF_MIN   = float(os.environ.get("CONF_MIN",  "0.82"))
MIN_SUP_G0 = int(os.environ.get("MIN_SUP_G0", "10"))
MIN_SUP_G1 = int(os.environ.get("MIN_SUP_G1", "8"))
N_MAX      = int(os.environ.get("N_MAX",      "4"))
Z_WILSON   = float(os.environ.get("Z_WILSON", "1.96"))

PUBLIC_SEND = os.environ.get("PUBLIC_SEND", "0") == "1"  # habilita envio por bot
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")  # chat id numérico do destino

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s",
)
log = logging.getLogger("fantan-auto")

# ----------------------------
# FastAPI
# ----------------------------
app = FastAPI(title="fantan-webhook")

# ----------------------------
# Telethon Client
# ----------------------------
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

@app.on_event("startup")
async def on_startup():
    # Conecta Telethon
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("SESSION_STRING inválida/expirada — refaça a sessão.")
    me = await client.get_me()
    log.info("📶 Retroativo conectado no Telegram com Telethon (%s)", me.username or me.id)

    # Resumo de setup
    log.info("✅ Retroativo inicial concluído.")
    log.info("ℹ️  Config: CONF_MIN=%.3f G0>=%d G1>=%d N_MAX=%d Z=%.2f",
             CONF_MIN, MIN_SUP_G0, MIN_SUP_G1, N_MAX, Z_WILSON)
    if PUBLIC_CHANNEL:
        log.info("🔎 Canal público alvo: %s", PUBLIC_CHANNEL)
    else:
        log.warning("⚠️  PUBLIC_CHANNEL não definido. /scrape não fará nada.")

# ----------------------------
# Helpers de sinais (placeholder simples)
# ----------------------------
def score_signal_from_text(text: str):
    """
    Exemplo simplificado:
    - procura padrões “G0”, “G1”, somas “+1,+2,+3...”, etc.
    - aqui você encaixa seu analisador real
    """
    t = text.lower()
    support_g0 = t.count("g0")
    support_g1 = t.count("g1")
    ns = sum(1 for x in t.split() if x.strip("+").isdigit())
    # confiança “fake” baseada em densidade de termos
    conf = min(0.5 + 0.1 * (support_g0 + support_g1) + 0.05 * ns, 0.99)
    return {
        "conf": conf,
        "support_g0": support_g0,
        "support_g1": support_g1,
        "n": max(1, min(ns, 8)),
    }

def passes_filters(sig: dict) -> bool:
    return (
        sig["conf"] >= CONF_MIN and
        sig["support_g0"] >= MIN_SUP_G0 and
        sig["support_g1"] >= MIN_SUP_G1 and
        sig["n"] <= N_MAX
    )

async def maybe_send_public(msg: str):
    """Envia via bot (opcional) se PUBLIC_SEND=1 + TG_BOT_TOKEN + TG_CHAT_ID."""
    if not (PUBLIC_SEND and TG_BOT_TOKEN and TG_CHAT_ID):
        return
    try:
        # Envia via Bot API (chamada direta simples)
        import httpx
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
        async with httpx.AsyncClient(timeout=20) as hx:
            r = await hx.post(url, json=payload)
            if r.status_code != 200:
                log.warning("Bot send falhou: %s %s", r.status_code, r.text)
    except Exception as e:
        log.warning("Falha no envio por bot: %s", e)

# ----------------------------
# Models
# ----------------------------
class IngestBody(BaseModel):
    text: str
    meta: Optional[dict] = None

class ScrapeBody(BaseModel):
    limit: int = SCRAPE_LIMIT
    channel: Optional[str] = None  # se quiser sobrescrever PUBLIC_CHANNEL

# ----------------------------
# Rotas
# ----------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

@app.post("/ingest")
async def ingest(body: IngestBody):
    """
    Recebe 1 mensagem (manual/webhook) e processa como possível sinal.
    """
    sig = score_signal_from_text(body.text)
    ok = passes_filters(sig)
    log.info("INGEST conf=%.3f g0=%d g1=%d n=%d -> %s",
             sig["conf"], sig["support_g0"], sig["support_g1"], sig["n"], "APROVADO" if ok else "descartado")
    if ok:
        msg = f"✅ <b>Sinal Aprovado</b>\nconf={sig['conf']:.2f} | g0={sig['support_g0']} g1={sig['support_g1']} n={sig['n']}\n\n<pre>{body.text[:2000]}</pre>"
        await maybe_send_public(msg)
    return {"approved": ok, "signal": sig}

@app.post("/scrape")
async def scrape(body: ScrapeBody):
    """
    Varre mensagens recentes do canal público e tenta aprovar sinais.
    """
    channel = body.channel or PUBLIC_CHANNEL
    if not channel:
        raise HTTPException(400, "Defina PUBLIC_CHANNEL no ambiente ou envie em body.channel")

    count = 0
    approved = 0
    try:
        async for msg in client.iter_messages(entity=channel, limit=max(1, min(body.limit, 2000))):
            if not isinstance(msg, Message):
                continue
            text = (msg.message or "").strip()
            if not text:
                continue
            count += 1
            sig = score_signal_from_text(text)
            if passes_filters(sig):
                approved += 1
                out = (
                    f"✅ <b>Sinal do retroativo</b>\n"
                    f"conf={sig['conf']:.2f} | g0={sig['support_g0']} g1={sig['support_g1']} n={sig['n']}\n"
                    f"<i>msg_id</i>={msg.id} • <i>data</i>={msg.date}\n\n"
                    f"<pre>{text[:2000]}</pre>"
                )
                await maybe_send_public(out)

        log.info("SCRAPE canal=%s lidos=%d aprovados=%d", channel, count, approved)
        return {"channel": channel, "read": count, "approved": approved}
    except FloodWaitError as fw:
        log.warning("FloodWait %ss no scrape", fw.seconds)
        raise HTTPException(429, f"Flood wait {fw.seconds}s")
    except RpcError as e:
        log.error("Erro Telegram RPC: %s", e)
        raise HTTPException(502, f"Telegram RPC error: {e}")
    except Exception as e:
        log.exception("Falha no scrape")
        raise HTTPException(500, f"Erro no scrape: {e}")

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    """
    Endpoint para receber webhook do seu Bot (opcional).
    Se quiser, configure no BotFather e aponte para /webhook/<seu_token_curto>.
    Aqui só registramos recebimento e, se houver 'text', aplicamos o mesmo motor.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    text = ""
    # compatível com Telegram Webhook padrão (update.message.text)
    msg = payload.get("message") or {}
    text = (msg.get("text") or "").strip()

    if not text:
        return {"ok": True, "note": "sem texto"}

    sig = score_signal_from_text(text)
    ok = passes_filters(sig)
    log.info("WEBHOOK conf=%.3f g0=%d g1=%d n=%d -> %s",
             sig["conf"], sig["support_g0"], sig["support_g1"], sig["n"], "APROVADO" if ok else "descartado")
    if ok:
        await maybe_send_public(
            f"✅ <b>Sinal (webhook)</b>\nconf={sig['conf']:.2f} | g0={sig['support_g0']} g1={sig['support_g1']} n={sig['n']}\n\n<pre>{text[:2000]}</pre>"
        )
    return {"approved": ok, "signal": sig}

# ----------------------------
# Tarefa automática de scraping (loop)
# ----------------------------
async def auto_scraper():
    await app.router.startup()
    # só roda se houver PUBLIC_CHANNEL
    if not PUBLIC_CHANNEL:
        log.warning("auto_scraper desativado: PUBLIC_CHANNEL vazio.")
        return

    while True:
        try:
            await scrape(ScrapeBody(limit=SCRAPE_LIMIT))   # chama a própria rota internamente
        except Exception as e:
            log.warning("auto_scraper erro: %s", e)
        await asyncio.sleep(SCRAPE_EVERY_S)

@app.on_event("startup")
async def _start_bg():
    # dispara o loop de scraping em background
    asyncio.create_task(auto_scraper())
    log.info("🧵 Loop de scraping automático iniciado (cada %ss, limit=%d).",
             SCRAPE_EVERY_S, SCRAPE_LIMIT)

# Para uvicorn: "webhook_app:app"