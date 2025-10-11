import os, re, json, asyncio, pytz, logging, hashlib, httpx
from datetime import datetime, timedelta
from collections import deque
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ================== CONFIGURAÇÃO ==================
TG_BOT_TOKEN   = os.environ.get("TG_BOT_TOKEN", "SEU_TOKEN_AQUI")
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID", "-1000000000000"))
TZ_NAME        = os.getenv("TZ", "America/Sao_Paulo")
LOCAL_TZ       = pytz.timezone(TZ_NAME)

TIPMINER_URL  = os.getenv("TIPMINER_URL", "https://www.tipminer.com/br/historico/jonbet/bac-bo")
FALLBACK_URL  = os.getenv("FALLBACK_URL", "https://casinoscores.com/pt-br/bac-bo/")

HEARTBEAT_SEC = int(os.getenv("HEARTBEAT_SEC", "3"))
AUTO_OPEN_INTERVAL_SEC = int(os.getenv("AUTO_OPEN_INTERVAL_SEC", "3"))
OPEN_TTL_SEC = int(os.getenv("OPEN_TTL_SEC", "90"))
CLOSE_STUCK_AFTER_SEC = int(os.getenv("CLOSE_STUCK_AFTER_SEC", "180"))

API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI()
log = logging.getLogger("uvicorn.error")

def now_local(): return datetime.now(LOCAL_TZ)

STATE = {
    "open_signal": None,
    "totals": {"greens": 0, "reds": 0},
    "rules_recent3": deque(maxlen=3),
    "last_side": None,
    "auto_last_round_sig": None
}

# ================== AUXILIARES ==================
async def tg_send(chat_id:int, text:str):
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            await cli.post(f"{API}/sendMessage", json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            })
    except Exception as e:
        log.error(f"Erro ao enviar para Telegram: {e}")

def _round_signature_from_html(html: str) -> str | None:
    if not html: return None
    head = html[:6000]
    return hashlib.sha1(head.encode("utf-8", errors="ignore")).hexdigest()

TM_PLAYER_RE = re.compile(r"(🔵|🟦|Player|Azul)", re.I)
TM_BANKER_RE = re.compile(r"(🔴|🟥|Banker|Vermelho)", re.I)

def _pick_side_from_html(html: str) -> str | None:
    if not html: return None
    head = html[:20000]
    has_p = bool(TM_PLAYER_RE.search(head))
    has_b = bool(TM_BANKER_RE.search(head))
    if has_p and not has_b: return "player"
    if has_b and not has_p: return "banker"
    mp = TM_PLAYER_RE.search(head)
    mb = TM_BANKER_RE.search(head)
    if mp and mb:
        return "player" if mp.start() < mb.start() else "banker"
    return None

async def _fetch(url: str) -> str | None:
    ts = int(now_local().timestamp())
    sep = "&" if "?" in url else "?"
    u = f"{url}{sep}_t={ts}"
    try:
        async with httpx.AsyncClient(timeout=15, headers={
            "User-Agent": "Mozilla/5.0",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache"
        }) as cli:
            r = await cli.get(u)
            if r.status_code == 200 and r.text:
                return r.text
    except Exception as e:
        log.warning(f"Falha ao buscar {url}: {e}")
    return None

# ================== LOOP PRINCIPAL ==================
async def tipminer_snapshot():
    html = await _fetch(TIPMINER_URL)
    sig = _round_signature_from_html(html or "")
    side = _pick_side_from_html(html or "")
    if not side:
        fb_html = await _fetch(FALLBACK_URL)
        side = _pick_side_from_html(fb_html or "")
        if not sig and fb_html:
            sig = _round_signature_from_html(fb_html)
    return sig, side

async def heartbeat_loop():
    while True:
        try:
            if STATE["open_signal"]:
                opened = STATE["open_signal"]["opened"]
                if (datetime.now() - opened).seconds > CLOSE_STUCK_AFTER_SEC:
                    STATE["open_signal"] = None
                    await tg_send(TARGET_CHAT_ID, "⏳ Encerrado por timeout (TTL)")
        except Exception as e:
            log.error(f"Erro no heartbeat: {e}")
        await asyncio.sleep(HEARTBEAT_SEC)

async def auto_loop():
    while True:
        try:
            sig, side = await tipminer_snapshot()
            if not sig: 
                await asyncio.sleep(AUTO_OPEN_INTERVAL_SEC)
                continue

            if STATE["auto_last_round_sig"] != sig:
                STATE["auto_last_round_sig"] = sig
                if side:
                    color = "🔵" if side == "player" else "🔴"
                    msg = f"🎲 <b>Nova rodada detectada</b>\nÚltimo lado: {color}"
                    await tg_send(TARGET_CHAT_ID, msg)
        except Exception as e:
            log.error(f"Erro no loop auto: {e}")
        await asyncio.sleep(AUTO_OPEN_INTERVAL_SEC)

# ================== STARTUP ==================
@app.on_event("startup")
async def start_tasks():
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(auto_loop())

# ================== ROTAS ==================
@app.get("/")
async def root():
    return {"ok": True, "service": "TipMiner 100% Automático", "tz": TZ_NAME}

@app.get("/debug/tipminer")
async def dbg_tipminer():
    sig, last_side = await tipminer_snapshot()
    return {"ok": True, "signature": sig, "last_side": last_side, "recent3": list(STATE["rules_recent3"])}

@app.get("/debug/state")
async def dbg_state():
    return {
        "open_signal": STATE.get("open_signal"),
        "totals": STATE["totals"],
        "last_side": STATE["last_side"],
        "recent3": list(STATE["rules_recent3"]),
    }

@app.get("/debug/force-close")
async def dbg_force_close():
    STATE["open_signal"] = None
    await tg_send(TARGET_CHAT_ID, "⏹ Fechamento forçado (debug)")
    return {"ok": True, "msg": "force close executado"}