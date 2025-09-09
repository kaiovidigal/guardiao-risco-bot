import os
import re
import asyncio
import json
import logging
import time
from typing import List, Dict, Any, Optional
from collections import deque, Counter

import nest_asyncio
nest_asyncio.apply()

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import PeerChannel
from aiogram import Bot

# =========================
# Config & Globals
# =========================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()  # ex: @fantanvidigal
DESTINO_CHAT_ID = int(os.getenv("DESTINO_CHAT_ID", "0"))  # id numérico do chat destino
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()

# Thresholds e ritmo
CONF_MIN = float(os.getenv("CONF_MIN", "0.82"))
COOLDOWN_S = int(os.getenv("COOLDOWN_S", "6"))
RESULTS_WINDOW = int(os.getenv("RESULTS_WINDOW", "30"))

# Retroativo (scraper)
AUTO_SCRAPE = int(os.getenv("AUTO_SCRAPE", "1"))
SCRAPE_EVERY_S = int(os.getenv("SCRAPE_EVERY_S", "120"))
SCRAPE_LIMIT = int(os.getenv("SCRAPE_LIMIT", "800"))

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

# Estado
app = FastAPI()
log = logging.getLogger("fantan-auto")
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

client: Optional[TelegramClient] = None
bot: Optional[Bot] = None

# memória recente de resultados e escolhas
_hist = deque(maxlen=RESULTS_WINDOW)     # números sorteados observados (1..4)
_stats = {"hits": 0, "miss": 0, "total": 0}
_last_signal: Dict[str, Any] = {}       # guarda último sinal para conferência
_last_sent_ts = 0.0

# ======= util =======
def now_ts() -> float:
    return time.time()

def pretty_conf(p: float) -> str:
    try:
        return f"{p*100:.2f}%"
    except Exception:
        return "n/a"

# =========================
# Escolha de número seco
# =========================
def choose_single_number(candidatos: List[int], modo: str = "G2") -> Dict[str, Any]:
    """
    Heurística leve:
      1) frequência recente na janela (Counter de _hist)
      2) desempate favorece centro (2,3) > pontas (1,4)
      3) pequeno ajuste por modo (G1/G2)
    Retorna: {"numero": n, "conf": 0~1, "modo": modo}
    """
    if not candidatos:
        return {"numero": None, "conf": 0.0, "modo": modo}

    cnt = Counter(_hist)
    # rank base pela frequência
    ranked = sorted(candidatos, key=lambda n: (cnt.get(n, 0), n in (2,3), -abs(2.5-n)), reverse=True)
    escolhido = ranked[0]

    # confiança simples: fração da freq do escolhido + bônus de densidade
    total_seen = max(1, sum(cnt.values()))
    freq = cnt.get(escolhido, 0) / total_seen
    dens = sum(cnt.get(n, 0) for n in candidatos) / total_seen

    conf = 0.55*freq + 0.35*dens + 0.10*(1 if escolhido in (2,3) else 0)
    # ajuste pelo modo (G1 costuma ser mais conservador)
    if modo.upper() == "G1":
        conf += 0.03
    conf = max(0.0, min(conf, 0.99))

    return {"numero": escolhido, "conf": conf, "modo": modo.upper()}

# =========================
# Parser de mensagens do canal
# =========================
_re_confirm = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
_re_cands   = re.compile(r"(?:Ssh|KWOK)[^\d]*([1-4](?:[-–][1-4]){1,3})")
_re_gales   = re.compile(r"até\s+(?P<g>\d)\s+gale", re.I)
_re_green   = re.compile(r"\bGREEN\b", re.I)
_re_res_num = re.compile(r"\b(?:saiu|resultado|number)\s*[:\-]?\s*([1-4])", re.I)

def parse_signal(text: str) -> Optional[Dict[str, Any]]:
    if not _re_confirm.search(text or ""):
        return None
    m = _re_cands.search(text)
    if not m:
        return None
    seq = m.group(1).replace("–", "-")
    candidatos = [int(x) for x in seq.split("-") if x in "1234"]
    g = 2  # default
    gm = _re_gales.search(text)
    if gm:
        try:
            g = int(gm.group("g"))
        except Exception:
            pass
    modo = "G1" if g <= 1 else "G2"
    return {"candidatos": candidatos, "modo": modo}

def parse_result(text: str) -> Optional[Dict[str, Any]]:
    """Tenta extrair GREEN/RED e o número (se vier)."""
    if not text:
        return None
    if _re_green.search(text):
        # tenta achar número explícito (nem sempre vem)
        m = _re_res_num.search(text)
        n = int(m.group(1)) if m else None
        return {"green": True, "numero": n}
    # (Opcional) procurar 'RED' se o canal publicar
    if "RED" in text.upper():
        m = _re_res_num.search(text)
        n = int(m.group(1)) if m else None
        return {"green": False, "numero": n}
    return None

# =========================
# Envio pro destino (Telegram)
# =========================
async def tg_send(text: str):
    try:
        await bot.send_message(chat_id=DESTINO_CHAT_ID, text=text)
    except Exception as e:
        log.error("Falha ao enviar msg destino: %s", e)

async def send_seco(candidatos: List[int], modo: str, conf: float, seco: int):
    global _last_signal
    msg = (
        f"🎯 SECO: {seco}  (de {'–'.join(map(str, candidatos))} | modo={modo})\n"
        f"🔎 confiança: {pretty_conf(conf)} | janela: {RESULTS_WINDOW}\n"
        f"📊 hits: {_stats['hits']} | erros: {_stats['miss']} | total: {_stats['total']}"
    )
    await tg_send(msg)
    _last_signal = {"ts": now_ts(), "seco": seco, "candidatos": candidatos, "modo": modo}

async def send_score(prefix: str = "📊"):
    msg = f"{prefix} hits: {_stats['hits']} | erros: {_stats['miss']} | total: {_stats['total']}"
    await tg_send(msg)

# =========================
# Retroativo (opcional)
# =========================
async def retro_scrape_loop():
    if not AUTO_SCRAPE or not PUBLIC_CHANNEL:
        return
    await app_started.wait()
    ch = PUBLIC_CHANNEL
    log.info("fantan-auto: 🔁 loop retroativo ligado (%s)...", ch)
    while True:
        try:
            # varre mensagens mais recentes
            async for msg in client.iter_messages(ch, limit=SCRAPE_LIMIT):
                txt = msg.message or ""
                # 1) acúmulo simples de números (caso apareça explícito)
                res = _re_res_num.search(txt)
                if res:
                    _hist.append(int(res.group(1)))
                # 2) emite seco quando identificar um sinal (no scrape não disparamos seco p/ evitar duplicar do webhook)
                # (Deixamos apenas aprender histórico)
            log.info("INFORMAÇÕES | fantan-auto | Raspe concluído. Mensagens processadas: %d", SCRAPE_LIMIT)
        except Exception as e:
            log.error("retro scrape erro: %s", e)
        await asyncio.sleep(SCRAPE_EVERY_S)

# =========================
# Telegram webhook do Bot
# =========================
async def ensure_webhook():
    """Registra o webhook do Bot para receber posts do canal."""
    if not (TG_BOT_TOKEN and RENDER_EXTERNAL_URL):
        log.warning("Webhook do Bot NÃO registrado (faltando TG_BOT_TOKEN ou RENDER_EXTERNAL_URL).")
        return
    import aiohttp
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setWebhook"
    hook = f"{RENDER_EXTERNAL_URL}/webhook/{TG_BOT_TOKEN}"
    data = {"url": hook, "drop_pending_updates": True}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=data, timeout=20) as r:
                js = await r.json()
        ok = js.get("ok")
        if ok:
            log.info("fantan-auto: 🌐 Webhook registrado em %s", hook)
        else:
            log.warning("fantan-auto: falha ao registrar webhook: %s", js)
    except Exception as e:
        log.error("setWebhook erro: %s", e)

# Modelo p/ saúde
class Status(BaseModel):
    ok: bool
    conf: Dict[str, Any]
    scrape: Dict[str, Any]
    bot: Dict[str, Any]
    webhook: str

@app.get("/status")
async def status():
    return Status(
        ok=True,
        conf={
            "CONF_MIN": CONF_MIN,
            "COOLDOWN_S": COOLDOWN_S,
            "RESULTS_WINDOW": RESULTS_WINDOW,
        },
        scrape={
            "PUBLIC_CHANNEL": PUBLIC_CHANNEL,
            "SCRAPE_EVERY_S": SCRAPE_EVERY_S,
            "SCRAPE_LIMIT": SCRAPE_LIMIT,
            "AUTO_SCRAPE": AUTO_SCRAPE,
        },
        bot={"enabled": bool(TG_BOT_TOKEN)},
        webhook="/webhook",
    )

@app.post("/webhook/{token}")
async def webhook_dyn(token: str, request: Request):
    if token.strip() != TG_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except Exception:
        data = {}
    log.info("%s", json.dumps(data, ensure_ascii=False))

    # Telegram update → channel_post / message
    text = None
    if "channel_post" in data:
        text = data["channel_post"].get("text") or data["channel_post"].get("caption")
    elif "message" in data:
        text = data["message"].get("text") or data["message"].get("caption")
    else:
        return {"ok": True}

    if not text:
        return {"ok": True}

    # 1) aprender número do resultado, se explícito
    mr = parse_result(text)
    if mr:
        n = mr.get("numero")
        if n in (1, 2, 3, 4):
            _hist.append(n)
            # se temos last_signal, podemos tentar marcar hit/miss
            if _last_signal.get("seco") in (1, 2, 3, 4):
                if n == _last_signal["seco"] and mr.get("green", True):
                    _stats["hits"] += 1
                else:
                    # se vier RED e número diferente do seco → miss
                    if mr.get("green") is False or (_last_signal["seco"] != n):
                        _stats["miss"] += 1
                _stats["total"] = _stats["hits"] + _stats["miss"]
                await send_score("✅📊" if mr.get("green", True) else "❌📊")
        return {"ok": True}

    # 2) se é um SINAL → escolhe e envia seco
    sig = parse_signal(text)
    if sig:
        # cooldown para não floodar
        global _last_sent_ts
        if now_ts() - _last_sent_ts < COOLDOWN_S:
            return {"ok": True}

        candidatos = [n for n in sig["candidatos"] if n in (1,2,3,4)]
        if not candidatos:
            return {"ok": True}

        res = choose_single_number(candidatos, sig["modo"])
        if res["conf"] >= CONF_MIN and res["numero"] in (1,2,3,4):
            await send_seco(candidatos, res["modo"], res["conf"], res["numero"])
            _last_sent_ts = now_ts()
        return {"ok": True}

    return {"ok": True}

# =========================
# Startup do app
# =========================
app_started = asyncio.Event()

@app.on_event("startup")
async def on_startup():
    global client, bot
    # Conecta Telethon (para retroativo e futuras expansões)
    if API_ID and API_HASH and SESSION_STRING:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.start()
        log.info("fantan-auto: 📬 Retroativo conectado no Telegram com Telethon")
    else:
        log.warning("Telethon não inicializado (faltam credenciais).")

    if TG_BOT_TOKEN:
        bot = Bot(TG_BOT_TOKEN)
    else:
        raise RuntimeError("TG_BOT_TOKEN ausente.")

    # registra webhook do bot (se base pública informada)
    await ensure_webhook()

    # inicia loop retroativo
    if client and AUTO_SCRAPE:
        asyncio.create_task(retro_scrape_loop())

    app_started.set()
    log.info("INFORMAÇÕES: Inicialização do aplicativo concluída.")
    log.info("Seu serviço está ativo 🎉")

# Rodar localmente com: uvicorn webhook_app:app --host 0.0.0.0 --port $PORT