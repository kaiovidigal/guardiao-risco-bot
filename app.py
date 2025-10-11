# app.py — Auto-OPEN pelo TipMiner + FECHAMENTO 100% pela TipMiner + Heartbeat + Debug
import os, re, json, asyncio, pytz, logging, random, hashlib
from datetime import datetime, timedelta
from collections import deque
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

# ================== ENVs OBRIGATÓRIAS (já deve ter no Render) ==================
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]              # token do bot
SOURCE_CHAT_ID = int(os.environ.get("SOURCE_CHAT_ID", "0") or "0")  # opcional se não usar webhook-fonte
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])       # canal/grupo onde publica
TZ_NAME        = os.getenv("TZ", "UTC")
LOCAL_TZ       = pytz.timezone(TZ_NAME)

# ================== ABERTURA (aqui vamos usar TipMiner; webhook é opcional) ==================
OPEN_STRICT_SERIAL = os.getenv("OPEN_STRICT_SERIAL", "1") == "1"
MIN_GAP_SECS       = int(os.getenv("MIN_GAP_SECS", "6"))   # anti-spam entre aberturas

# ================== FECHAMENTO (só externo) ==================
MIN_RESULT_DELAY_SEC = int(os.getenv("MIN_RESULT_DELAY_SEC", "8"))  # evita pegar rodada anterior

# ================== TTL / JANELAS ==================
OPEN_TTL_SEC              = int(os.getenv("OPEN_TTL_SEC", "90"))
EXTEND_TTL_ON_ACTIVITY    = os.getenv("EXTEND_TTL_ON_ACTIVITY", "1") == "1"
RESULT_GRACE_EXTEND_SEC   = int(os.getenv("RESULT_GRACE_EXTEND_SEC", "25"))
TIMEOUT_CLOSE_POLICY      = os.getenv("TIMEOUT_CLOSE_POLICY", "skip")  # skip | loss
POST_TIMEOUT_NOTICE       = os.getenv("POST_TIMEOUT_NOTICE", "0") == "1"

MAX_OPEN_WINDOW_SEC   = int(os.getenv("MAX_OPEN_WINDOW_SEC", "150"))
EXTEND_ONLY_ON_RELEVANT = os.getenv("EXTEND_ONLY_ON_RELEVANT", "1") == "1"
CLOSE_STUCK_AFTER_SEC = int(os.getenv("CLOSE_STUCK_AFTER_SEC", "180"))

# ================== EXPLORAÇÃO / SCORES (usados para escolher lado quando necessário) ==================
EXPLORE_EPS    = float(os.getenv("EXPLORE_EPS", "0.35"))
EXPLORE_MARGIN = float(os.getenv("EXPLORE_MARGIN", "0.15"))
EPS_TIE        = float(os.getenv("EPS_TIE", "0.02"))
SIDE_STREAK_CAP= int(os.getenv("SIDE_STREAK_CAP", "3"))

ROLLING_MAX    = int(os.getenv("ROLLING_MAX", "600"))
TREND_LOOKBACK = int(os.getenv("TREND_LOOKBACK", "40"))

DISABLE_WINDOWS = os.getenv("DISABLE_WINDOWS", "0") == "1"
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "25"))
TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.88"))
TOP_HOURS_COUNT       = int(os.getenv("TOP_HOURS_COUNT", "6"))
W_WR, W_HOUR, W_TREND = 0.70, 0.20, 0.10

ROLL_BAN_AFTER_R = int(os.getenv("ROLL_BAN_AFTER_R", "0"))
ROLL_BAN_HOURS   = int(os.getenv("ROLL_BAN_HOURS", "2"))
MIN_SIDE_SAMPLES = int(os.getenv("MIN_SIDE_SAMPLES", "30"))
COOLDOWN_AFTER_LOSS_MIN = int(os.getenv("COOLDOWN_AFTER_LOSS_MIN","10"))

# ================== LOG / STATE ==================
FLOW_THROUGH    = os.getenv("FLOW_THROUGH", "0") == "1"
LOG_RAW         = os.getenv("LOG_RAW", "1") == "1"
DEBUG_TO_TARGET = os.getenv("DEBUG_TO_TARGET", "0") == "1"
STATE_PATH      = os.getenv("STATE_PATH", "./state/state.json")
API             = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ================== TIPMINER / FALLBACK ==================
USE_PLAYWRIGHT            = os.getenv("USE_PLAYWRIGHT", "1") == "1"
REQUIRE_EXTERNAL_CONFIRM  = os.getenv("REQUIRE_EXTERNAL_CONFIRM", "1") == "1"
TIPMINER_URL              = os.getenv("TIPMINER_URL", "https://www.tipminer.com/br/historico/jonbet/bac-bo")
FALLBACK_URL              = os.getenv("FALLBACK_URL", "https://casinoscores.com/pt-br/bac-bo/")

# ================== HEARTBEAT ==================
HEARTBEAT_SEC = int(os.getenv("HEARTBEAT_SEC", "3"))

# ================== AUTO-OPEN (100% TipMiner) ==================
AUTO_OPEN_FROM_TIPMINER = os.getenv("AUTO_OPEN_FROM_TIPMINER", "1") == "1"
AUTO_OPEN_INTERVAL_SEC  = int(os.getenv("AUTO_OPEN_INTERVAL_SEC", "2"))
AUTO_OPEN_STRATEGY      = os.getenv("AUTO_OPEN_STRATEGY", "opposite")  # opposite | follow | random
AUTO_OPEN_MIN_GAP_SEC   = int(os.getenv("AUTO_OPEN_MIN_GAP_SEC", "8"))

# ================== APP ==================
app = FastAPI()
log = logging.getLogger("uvicorn.error")

# ================== REGEX (apenas se usar webhook do fonte) ==================
SIDE_WORD_RE = re.compile(r"\b(player|banker|empate|azul|vermelho|blue|red|p\b|b\b)\b", re.I)
EMOJI_PLAYER_RE = re.compile(r"(🔵|🟦)", re.U)
EMOJI_BANKER_RE = re.compile(r"(🔴|🟥)", re.U)

def now_local(): return datetime.now(LOCAL_TZ)
def colorize_side(s): return {"player":"🔵 Player", "banker":"🔴 Banker", "empate":"🟡 Empate"}.get(s or "", "?")

# ================== STATE ==================
STATE = {
    "pattern_roll": {}, "recent_results": deque(maxlen=TREND_LOOKBACK),
    "totals": {"greens":0, "reds":0}, "streak_green": 0,
    "open_signal": None, "last_publish_ts": None,
    "processed_updates": deque(maxlen=500), "messages": [],
    "source_texts": deque(maxlen=120),
    "hour_stats": {}, "side_ban_until": {},
    "cooldown_until": None, "last_side": None, "last_side_streak": 0,
    # auto-open tipminer
    "auto_last_round_sig": None,   # assinatura do último "estado" visto
    "auto_last_open_ts": None,     # ISO do último auto-open
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
        data["source_texts"]=deque(data.get("source_texts",[]), maxlen=120)
        for k in ("hour_stats","side_ban_until"):
            data.setdefault(k, {})
        for k in ("last_side","last_side_streak","cooldown_until"):
            data.setdefault(k, None if k!="last_side_streak" else 0)
        for k in ("auto_last_round_sig","auto_last_open_ts"):
            data.setdefault(k, None)
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

# ================== MÉTRICAS / SCORES ==================
def _bump_hour(res:str):
    hh = now_local().strftime("%H")
    h = STATE["hour_stats"].setdefault(hh, {"g":0,"t":0})
    h["t"] += 1
    if res == "G": h["g"] += 1

def rolling_append(side:str,res:str):
    dq=STATE["pattern_roll"].setdefault(side, deque(maxlen=ROLLING_MAX))
    dq.append(res); _bump_hour(res)

def side_wr(side:str)->tuple[float,int]:
    dq=STATE["pattern_roll"].get(side, deque()); n=len(dq)
    if n==0: return 0.0,0
    return (sum(1 for x in dq if x=="G")/n), n

def is_top_hour_now()->bool:
    if DISABLE_WINDOWS: return True
    stats = STATE.get("hour_stats", {})
    elig = [(h,s) for h,s in stats.items() if s["t"] >= TOP_HOURS_MIN_SAMPLES]
    if not elig: return True
    ranked = sorted(elig, key=lambda kv: (kv[1]["g"]/kv[1]["t"]), reverse=True)
    top = []
    for h,s in ranked:
        wr = s["g"]/s["t"]
        if wr >= TOP_HOURS_MIN_WINRATE: top.append(h)
        if len(top) >= TOP_HOURS_COUNT: break
    return now_local().strftime("%H") in top if top else True

def hour_bonus()->float: return 1.0 if is_top_hour_now() else 0.0
def trend_bonus()->float:
    dq=list(STATE["recent_results"]); n=len(dq)
    if n==0: return 0.0
    wr=sum(1 for x in dq if x=="G")/n
    return max(0.0,min(1.0,wr))

def compute_scores():
    wr_p,n_p=side_wr("player"); wr_b,n_b=side_wr("banker")
    hb, tb = hour_bonus(), trend_bonus()
    s_p = max(0.0, min(1.0, (W_WR*wr_p)+(W_HOUR*hb)+(W_TREND*tb) + random.uniform(0,EPS_TIE)))
    s_b = max(0.0, min(1.0, (W_WR*wr_b)+(W_HOUR*hb)+(W_TREND*tb) + random.uniform(0,EPS_TIE)))
    if s_p >= s_b: return ("player", s_p, s_b, {"delta": s_p-s_b})
    else:          return ("banker", s_b, s_p, {"delta": s_b-s_p})

def pick_by_streak_fallback():
    return "player" if random.random()<0.5 else "banker"

def _open_age_secs():
    osig = STATE.get("open_signal")
    if not osig: return 0
    try:
        opened = int(osig.get("src_opened_epoch") or 0)
        now_ep = int(datetime.now(LOCAL_TZ).timestamp())
        return max(0, now_ep - opened)
    except Exception:
        return 0

def cooldown_active()->bool:
    cu = STATE.get("cooldown_until")
    return bool(cu and datetime.fromisoformat(cu) > now_local())

def start_cooldown():
    STATE["cooldown_until"] = (now_local() + timedelta(minutes=COOLDOWN_AFTER_LOSS_MIN)).isoformat()
    save_state()

# ================== TIPMINER: Playwright + fallbacks HTML ==================
try:
    if USE_PLAYWRIGHT:
        from tipminer_scraper import get_tipminer_latest_side as pw_tipminer
except Exception as _e:
    log.warning("Playwright indisponível: %s", _e)
    USE_PLAYWRIGHT = False

TM_PLAYER_RE = re.compile(r"(🔵|🟦|Player|Azul)", re.I)
TM_BANKER_RE = re.compile(r"(🔴|🟥|Banker|Vermelho)", re.I)
CS_PLAYER_RE = re.compile(r"(Jogador|Player)", re.I)
CS_BANKER_RE = re.compile(r"(Banqueiro|Banker)", re.I)

async def _fetch(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and r.text: return r.text
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

# -------- Snapshot/assinatura de rodada --------
def _round_signature_from_html(html: str) -> str | None:
    if not html: return None
    head = html[:6000]
    return hashlib.sha1(head.encode("utf-8", errors="ignore")).hexdigest()

async def tipminer_snapshot() -> tuple[str | None, str | None]:
    """
    Retorna (signature, last_side) a partir do TipMiner (ou fallback).
    - signature: muda quando a página muda (nova rodada)
    - last_side: 'player' | 'banker' (pode ser None)
    """
    # 1) Playwright (se disponível)
    if USE_PLAYWRIGHT:
        try:
            # seu scraper pode retornar (side, "tipminer_pw", raw_html)
            side, _src, raw_html = await pw_tipminer(TIPMINER_URL)
            sig = _round_signature_from_html(raw_html or "")
            if not sig:
                # se por algum motivo não veio o html, tenta HTML simples
                html = await _fetch(TIPMINER_URL)
                sig = _round_signature_from_html(html or "")
            return sig, side
        except Exception:
            pass

    # 2) HTML simples
    html = await _fetch(TIPMINER_URL)
    sig = _round_signature_from_html(html or "")
    side = _pick_side_from_html(html or "", TM_PLAYER_RE, TM_BANKER_RE)
    if not side:
        # fallback CasinoScores
        fb_html = await _fetch(FALLBACK_URL)
        if fb_html:
            side = _pick_side_from_html(fb_html, CS_PLAYER_RE, CS_BANKER_RE)
            if not sig:
                sig = _round_signature_from_html(fb_html)
    return sig, side

# ================== RESULTADO & PUBLICAÇÃO ==================
def _result_from_sides(chosen: str, real: str) -> str:
    # GREEN se nossa escolha != lado real do último giro
    return "G" if chosen and real and chosen != real else "R"

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

# ================== FECHAMENTO 100% EXTERNO ==================
async def _apply_result_external_only(chosen_side: str | None) -> str | None:
    if not REQUIRE_EXTERNAL_CONFIRM:
        return None
    # checa lado real no TipMiner (ou fallback)
    html = await _fetch(TIPMINER_URL)
    real_side = _pick_side_from_html(html or "", TM_PLAYER_RE, TM_BANKER_RE)
    if not real_side:
        fb_html = await _fetch(FALLBACK_URL)
        if fb_html:
            real_side = _pick_side_from_html(fb_html, CS_PLAYER_RE, CS_BANKER_RE)
    if not real_side:
        await aux_log({"ts": now_local().isoformat(), "type": "external_missing"})
        return None
    res = _result_from_sides(chosen_side, real_side)
    if DEBUG_TO_TARGET:
        await tg_send(TARGET_CHAT_ID, f"[DEBUG] real={real_side} -> {res}")
    return res

async def maybe_close_by_external():
    osig = STATE.get("open_signal")
    if not osig: return
    opened_epoch = osig.get("src_opened_epoch", 0)
    now_epoch = int(datetime.now(LOCAL_TZ).timestamp())
    # espera o delay mínimo pra evitar rodada anterior
    if opened_epoch and (now_epoch - opened_epoch) < MIN_RESULT_DELAY_SEC:
        return

    final_res = await _apply_result_external_only(osig.get("chosen_side"))
    if final_res is None:
        return

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
        await tg_send(TARGET_CHAT_ID, f"[HB] Fechado por heartbeat: {final_res}")

# ================== TTL / ANTI-TRAVA ==================
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
    if TIMEOUT_CLOSE_POLICY == "loss":
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0
        start_cooldown()
        await announce_outcome("R", None)
    elif POST_TIMEOUT_NOTICE:
        await tg_send(TARGET_CHAT_ID, "⏳ Encerrado por timeout (TTL) — descartado")

# ================== AUTO-OPEN 100% TIPMINER ==================
def _decide_entry_from_tipminer(last_side: str | None) -> str:
    if AUTO_OPEN_STRATEGY == "random" or not last_side:
        return "player" if random.random() < 0.5 else "banker"
    if AUTO_OPEN_STRATEGY == "follow":
        return last_side
    # opposite (padrão)
    return "player" if last_side == "banker" else "banker"

async def auto_open_loop():
    if not AUTO_OPEN_FROM_TIPMINER:
        return
    while True:
        try:
            sig, last_side = await tipminer_snapshot()
            if sig:
                prev_sig = STATE.get("auto_last_round_sig")
                now = now_local()

                # respeita espaçamento mínimo
                spaced_ok = True
                last_open_iso = STATE.get("auto_last_open_ts")
                if last_open_iso:
                    try:
                        spaced_ok = (now - datetime.fromisoformat(last_open_iso)).total_seconds() >= AUTO_OPEN_MIN_GAP_SEC
                    except:
                        pass

                # abre quando: sem sinal aberto + assinatura mudou + espaçamento ok
                if (STATE.get("open_signal") is None) and spaced_ok and (prev_sig is None or sig != prev_sig):
                    chosen = _decide_entry_from_tipminer(last_side)
                    fake_msg = {"message_id": 0, "date": int(now.timestamp()), "text": "[auto-open tipminer]"}
                    # anti-spam adicional
                    last_pub = STATE.get("last_publish_ts")
                    if last_pub:
                        try:
                            if (now - datetime.fromisoformat(last_pub)).total_seconds() < MIN_GAP_SECS:
                                # pula para evitar flood
                                STATE["auto_last_round_sig"] = sig
                                save_state()
                                await asyncio.sleep(AUTO_OPEN_INTERVAL_SEC)
                                continue
                        except:
                            pass
                    await publish_entry(chosen_side=chosen, fonte_side=None, msg=fake_msg)
                    STATE["auto_last_round_sig"] = sig
                    STATE["auto_last_open_ts"] = now.isoformat()
                    save_state()

                    if DEBUG_TO_TARGET:
                        await tg_send(TARGET_CHAT_ID, f"[AUTO-OPEN] sig mudou • last={last_side or '?'} • chosen={chosen}")

                # atualiza assinatura mesmo se não abriu (para acompanhar site)
                if prev_sig != sig and STATE.get("open_signal") is not None:
                    STATE["auto_last_round_sig"] = sig
                    save_state()

        except Exception as e:
            try:
                await aux_log({"ts": now_local().isoformat(), "type": "auto_open_err", "err": str(e)})
            except:
                pass
        await asyncio.sleep(AUTO_OPEN_INTERVAL_SEC)

# ================== HEARTBEAT ==================
async def heartbeat_loop():
    while True:
        try:
            # tenta fechar por confirmação externa
            await maybe_close_by_external()
            # TTL/anti-trava
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

# ================== FASTAPI/ROUTES ==================
@app.middleware("http")
async def safe_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        logging.exception("Middleware error: %s", e)
        return JSONResponse({"ok": True}, status_code=200)

@app.get("/")
async def root():
    return {"ok": True, "service": "TipMiner abre & fecha • Heartbeat ativo"}

# Endpoints de debug (opcionais)
@app.get("/debug/tipminer")
async def dbg_tm():
    try:
        sig, last_side = await tipminer_snapshot()
        return {"ok": True, "signature": sig, "last_side": last_side}
    except Exception as e:
        return {"ok": False, "err": str(e)}

@app.get("/debug/fallback")
async def dbg_fb():
    try:
        html = await _fetch(FALLBACK_URL)
        side = _pick_side_from_html(html or "", CS_PLAYER_RE, CS_BANKER_RE)
        return {"ok": True, "side": side}
    except Exception as e:
        return {"ok": False, "err": str(e)}

# Webhook continua existindo, mas é opcional (não depende dele p/ abrir/fechar)
@app.post("/webhook")
@app.post("/webhook/{secret}")
async def webhook(req: Request, secret: str|None=None):
    try:
        update = await req.json()
    except Exception:
        return JSONResponse({"ok": True})
    if LOG_RAW: log.info("RAW UPDATE: %s", update)
    up_id = update.get("update_id")
    if up_id is not None:
        if up_id in STATE["processed_updates"]: return JSONResponse({"ok": True})
        STATE["processed_updates"].append(up_id)

    msg = update.get("channel_post") or update.get("message") or {}
    chat = (msg.get("chat") or {})
    # se quiser filtrar por canal-fonte (opcional)
    if SOURCE_CHAT_ID and chat.get("id") != SOURCE_CHAT_ID:
        return JSONResponse({"ok": True})

    text = (msg.get("text") or msg.get("caption") or "").strip()
    if text:
        STATE["source_texts"].append(text); save_state()
        if FLOW_THROUGH: await tg_send(TARGET_CHAT_ID, text)

    # o coração do sistema está nos loops (auto_open_loop + heartbeat_loop)
    return JSONResponse({"ok": True})