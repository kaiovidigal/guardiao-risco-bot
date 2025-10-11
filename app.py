# app.py — TipMiner abre e fecha • Heartbeat • Anti-cache • Retry • GREEN_RULE • Force-close • Debug
import os, re, json, asyncio, pytz, logging, random, hashlib
from datetime import datetime, timedelta
from collections import deque
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

# ================== ENVs obrigatórias ==================
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])  # canal/grupo destino (ex: -100...)
TZ_NAME        = os.getenv("TZ", "UTC")
LOCAL_TZ       = pytz.timezone(TZ_NAME)

# ================== Config principal ==================
# Auto-open 100% TipMiner
AUTO_OPEN_FROM_TIPMINER = os.getenv("AUTO_OPEN_FROM_TIPMINER", "1") == "1"
AUTO_OPEN_INTERVAL_SEC  = int(os.getenv("AUTO_OPEN_INTERVAL_SEC", "2"))
AUTO_OPEN_STRATEGY      = os.getenv("AUTO_OPEN_STRATEGY", "opposite")  # opposite | follow | random
AUTO_OPEN_MIN_GAP_SEC   = int(os.getenv("AUTO_OPEN_MIN_GAP_SEC", "8"))

# Fechamento (externo)
REQUIRE_EXTERNAL_CONFIRM = os.getenv("REQUIRE_EXTERNAL_CONFIRM", "1") == "1"
MIN_RESULT_DELAY_SEC     = int(os.getenv("MIN_RESULT_DELAY_SEC", "8"))

# Regra de GREEN
GREEN_RULE = os.getenv("GREEN_RULE", "opposite").lower()  # opposite | follow

# Janelas / segurança
OPEN_TTL_SEC            = int(os.getenv("OPEN_TTL_SEC", "90"))
RESULT_GRACE_EXTEND_SEC = int(os.getenv("RESULT_GRACE_EXTEND_SEC", "25"))
MAX_OPEN_WINDOW_SEC     = int(os.getenv("MAX_OPEN_WINDOW_SEC", "150"))
CLOSE_STUCK_AFTER_SEC   = int(os.getenv("CLOSE_STUCK_AFTER_SEC", "180"))

# Heartbeat e logs
HEARTBEAT_SEC   = int(os.getenv("HEARTBEAT_SEC", "3"))
DEBUG_TO_TARGET = os.getenv("DEBUG_TO_TARGET", "0") == "1"
LOG_RAW         = os.getenv("LOG_RAW", "1") == "1"

# Exploração mínima (fallback se last_side não vier)
EXPLORE_EPS    = float(os.getenv("EXPLORE_EPS", "0.35"))
EXPLORE_MARGIN = float(os.getenv("EXPLORE_MARGIN", "0.15"))
EPS_TIE        = float(os.getenv("EPS_TIE", "0.02"))
SIDE_STREAK_CAP= int(os.getenv("SIDE_STREAK_CAP", "3"))

ROLLING_MAX    = int(os.getenv("ROLLING_MAX", "600"))
TREND_LOOKBACK = int(os.getenv("TREND_LOOKBACK", "40"))
COOLDOWN_AFTER_LOSS_MIN = int(os.getenv("COOLDOWN_AFTER_LOSS_MIN","10"))

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API        = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# TipMiner / Fallback
TIPMINER_URL = os.getenv("TIPMINER_URL", "https://www.tipminer.com/br/historico/jonbet/bac-bo")
FALLBACK_URL = os.getenv("FALLBACK_URL", "https://casinoscores.com/pt-br/bac-bo/")
USE_PLAYWRIGHT = os.getenv("USE_PLAYWRIGHT", "0") == "1"  # por padrão 0 (HTML simples + anti-cache)

# ================== App ==================
app = FastAPI()
log = logging.getLogger("uvicorn.error")

def now_local(): return datetime.now(LOCAL_TZ)
def colorize_side(s): return {"player":"🔵 Player", "banker":"🔴 Banker", "empate":"🟡 Empate"}.get(s or "", "?")

# ================== STATE ==================
STATE = {
    "pattern_roll": {}, "recent_results": deque(maxlen=TREND_LOOKBACK),
    "totals": {"greens":0, "reds":0}, "streak_green": 0,
    "open_signal": None, "last_publish_ts": None,
    "processed_updates": deque(maxlen=500), "messages": [],
    # auto-open tipminer
    "auto_last_round_sig": None,
    "auto_last_open_ts": None,
    "last_side": None, "last_side_streak": 0,
    "cooldown_until": None,
    "hour_stats": {},
}
def _ensure_dir(p):
    d=os.path.dirname(p)
    if d and not os.path.exists(d): os.makedirs(d, exist_ok=True)

def _jsonable(o):
    if isinstance(o,deque): return list(o)
    if isinstance(o,dict): return {k:_jsonable(v) for k,v in o.items()}
    if isinstance(o,list): return [_jsonable(x) for x in o]
    return o

def save_state():
    _ensure_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(_jsonable(STATE), f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)

def load_state():
    global STATE
    try:
        with open(STATE_PATH,"r",encoding="utf-8") as f: data=json.load(f)
        pr=data.get("pattern_roll",{})
        for k,v in list(pr.items()):
            try: pr[k]=deque(v,maxlen=ROLLING_MAX)
            except: pr[k]=deque(maxlen=ROLLING_MAX)
        data["pattern_roll"]=pr
        data["recent_results"]=deque(data.get("recent_results",[]), maxlen=TREND_LOOKBACK)
        for k in ("hour_stats","auto_last_round_sig","auto_last_open_ts","cooldown_until","last_side","last_side_streak"):
            data.setdefault(k, None if k not in ("hour_stats","last_side_streak") else ({} if k=="hour_stats" else 0))
        STATE.update(data)
    except Exception:
        save_state()
load_state()

async def tg_send(chat_id:int, text:str):
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            await cli.post(f"{API}/sendMessage",
                json={"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":True})
    except Exception as e:
        log.error("tg_send error: %s", e)

async def aux_log(e:dict):
    STATE["messages"].append(e)
    if len(STATE["messages"])>5000: STATE["messages"]=STATE["messages"][-4000:]
    save_state()

# ================== Métricas simples ==================
def _bump_hour(res:str):
    hh = now_local().strftime("%H")
    h = STATE["hour_stats"].setdefault(hh, {"g":0,"t":0})
    h["t"] += 1
    if res == "G": h["g"] += 1

def rolling_append(side:str,res:str):
    dq=STATE["pattern_roll"].setdefault(side, deque(maxlen=ROLLING_MAX))
    dq.append(res); _bump_hour(res)

def start_cooldown():
    STATE["cooldown_until"] = (now_local() + timedelta(minutes=COOLDOWN_AFTER_LOSS_MIN)).isoformat()
    save_state()

def _open_age_secs():
    osig = STATE.get("open_signal")
    if not osig: return 0
    try:
        opened = int(osig.get("src_opened_epoch") or 0)
        now_ep = int(now_local().timestamp())
        return max(0, now_ep - opened)
    except Exception:
        return 0

# ================== DETECÇÃO DE LADO no HTML ==================
TM_PLAYER_RE = re.compile(r"(🔵|🟦|Player|Azul)", re.I)
TM_BANKER_RE = re.compile(r"(🔴|🟥|Banker|Vermelho)", re.I)
CS_PLAYER_RE = re.compile(r"(Jogador|Player)", re.I)
CS_BANKER_RE = re.compile(r"(Banqueiro|Banker)", re.I)

# -------- FETCH ANTI-CACHE --------
async def _fetch(url: str) -> str | None:
    ts = int(now_local().timestamp())
    sep = "&" if "?" in url else "?"
    u = f"{url}{sep}_t={ts}"
    try:
        async with httpx.AsyncClient(timeout=15, headers={
            "User-Agent": "Mozilla/5.0",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Accept": "text/html,application/xhtml+xml"
        }) as cli:
            r = await cli.get(u)
            if r.status_code == 200 and r.text:
                return r.text
    except Exception as e:
        log.warning("fetch fail %s: %s", url, e)
    return None

def _pick_side_from_html(html: str, re_player: re.Pattern, re_banker: re.Pattern) -> str | None:
    if not html: return None
    head = html[:20000]
    has_p = bool(re_player.search(head)); has_b = bool(re_banker.search(head))
    if has_p and not has_b: return "player"
    if has_b and not has_p: return "banker"
    mp = re_player.search(head); mb = re_banker.search(head)
    if mp and mb: return "player" if mp.start() < mb.start() else "banker"
    return None

def _round_signature_from_html(html: str) -> str | None:
    if not html: return None
    head = html[:6000]
    return hashlib.sha1(head.encode("utf-8", errors="ignore")).hexdigest()

async def tipminer_snapshot() -> tuple[str | None, str | None]:
    # HTML simples (anti-cache)
    html = await _fetch(TIPMINER_URL)
    sig  = _round_signature_from_html(html or "")
    side = _pick_side_from_html(html or "", TM_PLAYER_RE, TM_BANKER_RE)
    if not side:
        fb_html = await _fetch(FALLBACK_URL)
        if fb_html:
            side = _pick_side_from_html(fb_html, CS_PLAYER_RE, CS_BANKER_RE)
            if not sig:
                sig = _round_signature_from_html(fb_html)
    # (Opcional) Playwright se habilitado
    if not side and USE_PLAYWRIGHT:
        try:
            from tipminer_scraper import get_tipminer_latest_side
            side2, _src, raw = await get_tipminer_latest_side(TIPMINER_URL)
            sig2 = _round_signature_from_html(raw or "")
            return sig2 or sig, side2
        except Exception as e:
            await aux_log({"ts": now_local().isoformat(), "type": "pw_err", "err": str(e)})
    return sig, side

# ================== Resultado & Publicação ==================
def _result_from_sides(chosen: str, real: str) -> str:
    """
    Decide GREEN/LOSS conforme GREEN_RULE:
    - opposite: GREEN se chosen != real
    - follow:   GREEN se chosen == real
    """
    rule = GREEN_RULE
    if not chosen or not real:
        return "R"
    if rule == "follow":
        return "G" if chosen == real else "R"
    # padrão: opposite
    return "G" if chosen != real else "R"

async def publish_entry(chosen_side:str, fonte_side:str|None, msg:dict):
    pretty = ("🚀 <b>ENTRADA AUTÔNOMA (G0)</b>\n"
              f"{colorize_side(chosen_side)}\n"
              f"Origem: TipMiner")
    await tg_send(TARGET_CHAT_ID, pretty)
    STATE["open_signal"] = {
        "ts": now_local().isoformat(),
        "chosen_side": chosen_side,
        "fonte_side": fonte_side,
        "expires_at": (now_local()+timedelta(seconds=OPEN_TTL_SEC)).isoformat(),
        "src_msg_id": msg.get("message_id", 0),
        "src_opened_epoch": msg.get("date", int(now_local().timestamp())),
    }

    # guarda assinatura da rodada no momento da abertura
    try:
        html_open = await _fetch(TIPMINER_URL)
        STATE["open_signal"]["open_signature"] = _round_signature_from_html(html_open or "")
    except Exception:
        STATE["open_signal"]["open_signature"] = None

    if STATE.get("last_side") == chosen_side:
        STATE["last_side_streak"] = STATE.get("last_side_streak",0) + 1
    else:
        STATE["last_side_streak"] = 1
    STATE["last_side"] = chosen_side
    STATE["last_publish_ts"] = now_local().isoformat()
    save_state()

async def announce_outcome(result:str, chosen_side:str|None):
    big = "🟩🟩🟩 <b>GREEN</b> 🟩🟩🟩" if result=="G" else "🟥🟥🟥 <b>LOSS</b> 🟥🟥🟥"
    await tg_send(TARGET_CHAT_ID, f"{big}\n⏱ {now_local().strftime('%H:%M:%S')}\nNossa: {colorize_side(chosen_side)}")
    g=STATE["totals"]["greens"]; r=STATE["totals"]["reds"]; t=g+r; wr=(g/t*100.0) if t else 0.0
    await tg_send(TARGET_CHAT_ID, f"📊 <b>Placar Geral</b>\n✅ {g}   ⛔️ {r}\n🎯 {wr:.2f}%  •  🔥 Streak {STATE['streak_green']}")

# ================== Fechamento externo (Retry + Logs) ==================
async def _apply_result_external_only(chosen_side: str | None) -> str | None:
    # modo SOS: fecha mesmo sem site (apenas quando REQUIRE_EXTERNAL_CONFIRM=0)
    if not REQUIRE_EXTERNAL_CONFIRM:
        return "G" if int(now_local().timestamp()) % 2 == 0 else "R"

    attempts = []
    for i in range(3):
        # TipMiner
        html = await _fetch(TIPMINER_URL)
        real = _pick_side_from_html(html or "", TM_PLAYER_RE, TM_BANKER_RE)
        attempts.append({"try": i+1, "site": "tipminer", "side": real})

        if not real:
            # Fallback
            fb_html = await _fetch(FALLBACK_URL)
            real = _pick_side_from_html(fb_html or "", CS_PLAYER_RE, CS_BANKER_RE)
            attempts.append({"try": i+1, "site": "fallback", "side": real})

        if real:
            res = _result_from_sides(chosen_side, real)
            if DEBUG_TO_TARGET:
                await tg_send(TARGET_CHAT_ID, f"[DEBUG] try={i+1} real={real} -> {res} (rule={GREEN_RULE})")
            return res

        await asyncio.sleep(1.2)

    if DEBUG_TO_TARGET:
        await tg_send(TARGET_CHAT_ID, f"[DEBUG] sem cor no site após tentativas: {attempts}")
    await aux_log({"ts": now_local().isoformat(), "type": "external_missing", "attempts": attempts})
    return None

# ================== Fechar quando assinatura muda + delay mínimo ==================
async def maybe_close_by_external():
    osig = STATE.get("open_signal")
    if not osig:
        return

    # assinatura mudou? então a rodada antiga já foi resolvida
    signature_changed = False
    try:
        html_now = await _fetch(TIPMINER_URL)
        sig_now  = _round_signature_from_html(html_now or "")
        sig_open = osig.get("open_signature")
        signature_changed = (sig_open and sig_now and sig_open != sig_now)
    except Exception:
        pass

    opened_epoch = osig.get("src_opened_epoch", 0)
    now_epoch = int(now_local().timestamp())

    # respeita delay mínimo, exceto se a assinatura já mudou
    if not signature_changed and opened_epoch and (now_epoch - opened_epoch) < MIN_RESULT_DELAY_SEC:
        return

    # tenta ler a cor (com retry)
    final_res = await _apply_result_external_only(osig.get("chosen_side"))
    if final_res is None:
        # rodada virou mas sem cor? tenta de novo no próximo loop
        if signature_changed and DEBUG_TO_TARGET:
            await tg_send(TARGET_CHAT_ID, "[DEBUG] rodada virou, aguardando cor no próximo heartbeat")
        return

    # aplica placar e fecha
    if final_res == "G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] += 1
        rolling_append(osig.get("chosen_side"), "G")
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0
        rolling_append(osig.get("chosen_side"), "R")
        start_cooldown()

    await announce_outcome(final_res, osig.get("chosen_side"))
    STATE["open_signal"] = None
    save_state()
    if DEBUG_TO_TARGET:
        await tg_send(TARGET_CHAT_ID, f"[HB] Fechado (sig_changed={signature_changed}) -> {final_res}")

# ================== TTL / Anti-trava ==================
async def expire_open_if_needed():
    osig = STATE.get("open_signal")
    if not osig: return
    if _open_age_secs() >= CLOSE_STUCK_AFTER_SEC:
        STATE["open_signal"] = None
        save_state(); return
    exp = osig.get("expires_at")
    if not exp: return
    if datetime.fromisoformat(exp) > now_local(): return
    STATE["open_signal"] = None
    save_state()
    await tg_send(TARGET_CHAT_ID, "⏳ Encerrado por timeout (TTL) — descartado")

# ================== Auto-open 100% TipMiner ==================
def _decide_entry_from_tipminer(last_side: str | None) -> str:
    if AUTO_OPEN_STRATEGY == "random" or not last_side:
        return "player" if random.random() < 0.5 else "banker"
    if AUTO_OPEN_STRATEGY == "follow":
        return last_side
    return "player" if last_side == "banker" else "banker"  # opposite

async def auto_open_loop():
    if not AUTO_OPEN_FROM_TIPMINER:
        return
    while True:
        try:
            sig, last_side = await tipminer_snapshot()
            if sig:
                prev_sig = STATE.get("auto_last_round_sig")
                now = now_local()

                # espaçamento mínimo
                spaced_ok = True
                last_open_iso = STATE.get("auto_last_open_ts")
                if last_open_iso:
                    try:
                        spaced_ok = (now - datetime.fromisoformat(last_open_iso)).total_seconds() >= AUTO_OPEN_MIN_GAP_SEC
                    except:
                        pass

                # abre: sem sinal aberto + assinatura mudou + espaçamento ok
                if (STATE.get("open_signal") is None) and spaced_ok and (prev_sig is None or sig != prev_sig):
                    chosen = _decide_entry_from_tipminer(last_side)
                    fake_msg = {"message_id": 0, "date": int(now.timestamp()), "text": "[auto-open tipminer]"}
                    await publish_entry(chosen_side=chosen, fonte_side=None, msg=fake_msg)
                    STATE["auto_last_round_sig"] = sig
                    STATE["auto_last_open_ts"] = now.isoformat()
                    save_state()
                    if DEBUG_TO_TARGET:
                        await tg_send(TARGET_CHAT_ID, f"[AUTO-OPEN] sig mudou • last={last_side or '?'} • chosen={chosen}")

                # com sinal aberto, ainda assim atualiza a assinatura (p/ detectar virada)
                if prev_sig != sig and STATE.get("open_signal") is not None:
                    STATE["auto_last_round_sig"] = sig
                    save_state()

        except Exception as e:
            try:
                await aux_log({"ts": now_local().isoformat(), "type": "auto_open_err", "err": str(e)})
            except:
                pass
        await asyncio.sleep(AUTO_OPEN_INTERVAL_SEC)

# ================== Heartbeat ==================
async def heartbeat_loop():
    while True:
        try:
            await maybe_close_by_external()
            await expire_open_if_needed()
        except Exception as e:
            try:
                await aux_log({"ts": now_local().isoformat(), "type": "hb_err", "err": str(e)})
            except:
                pass
        await asyncio.sleep(HEARTBEAT_SEC)

@app.on_event("startup")
async def _start_background_tasks():
    asyncio.create_task(heartbeat_loop())
    if AUTO_OPEN_FROM_TIPMINER:
        asyncio.create_task(auto_open_loop())

# ================== FastAPI / Rotas ==================
@app.middleware("http")
async def safe_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        logging.exception("Middleware error: %s", e)
        return JSONResponse({"ok": True}, status_code=200)

@app.get("/")
async def root():
    return {"ok": True, "service": "TipMiner abre & fecha • Heartbeat • Anti-cache • Retry • GREEN_RULE"}

@app.get("/debug/tipminer")
async def dbg_tm():
    try:
        sig, last_side = await tipminer_snapshot()
        return {"ok": True, "signature": sig, "last_side": last_side}
    except Exception as e:
        return {"ok": False, "err": str(e)}

@app.get("/debug/state")
async def dbg_state():
    return {"open_signal": STATE.get("open_signal"), "last_publish_ts": STATE.get("last_publish_ts")}

# ---- Force-close (para teste rápido pelo celular) ----
@app.get("/debug/force-close")
async def dbg_force_close():
    osig = STATE.get("open_signal")
    if not osig:
        return {"ok": False, "err": "no open_signal"}
    res = await _apply_result_external_only(osig.get("chosen_side"))
    if not res:
        return {"ok": False, "err": "no external color"}
    if res == "G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] += 1
        rolling_append(osig.get("chosen_side"), "G")
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0
        rolling_append(osig.get("chosen_side"), "R")
        start_cooldown()
    await announce_outcome(res, osig.get("chosen_side"))
    STATE["open_signal"] = None
    save_state()
    if DEBUG_TO_TARGET:
        await tg_send(TARGET_CHAT_ID, f"[DEBUG] force-close: {res}")
    return {"ok": True, "result": res}