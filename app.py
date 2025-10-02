# app.py — G0 serial + fechamento rápido vs fonte + 5 especialistas + NEUTRO correto
import os, re, json, asyncio, pytz, logging, random
from datetime import datetime, timedelta
from collections import deque
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

# ================== ENVs ==================
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
TZ_NAME        = os.getenv("TZ", "UTC")
LOCAL_TZ       = pytz.timezone(TZ_NAME)

# Abertura / Serial
OPEN_STRICT_SERIAL = os.getenv("OPEN_STRICT_SERIAL", "1") == "1"
MIN_GAP_SECS       = int(os.getenv("MIN_GAP_SECS", "6"))
FORCE_TRIGGER_OPEN = os.getenv("FORCE_TRIGGER_OPEN", "1") == "1"
FORCE_OPEN_ON_ANY_SOURCE_MSG = os.getenv("FORCE_OPEN_ON_ANY_SOURCE_MSG", "1") == "1"

# Fechamento (sempre pelo primeiro desfecho do fonte)
CLOSE_ONLY_ON_FLOW_CONFIRM = os.getenv("CLOSE_ONLY_ON_FLOW_CONFIRM", "1") == "1"
ENFORCE_RESULT_AFTER_NEW_SOURCE_MESSAGE = os.getenv("ENFORCE_RESULT_AFTER_NEW_SOURCE_MESSAGE", "1") == "1"
MIN_RESULT_DELAY_SEC = int(os.getenv("MIN_RESULT_DELAY_SEC", "8"))

# TTL / anti-trava
OPEN_TTL_SEC              = int(os.getenv("OPEN_TTL_SEC", "90"))
EXTEND_TTL_ON_ACTIVITY    = os.getenv("EXTEND_TTL_ON_ACTIVITY", "1") == "1"
RESULT_GRACE_EXTEND_SEC   = int(os.getenv("RESULT_GRACE_EXTEND_SEC", "25"))
TIMEOUT_CLOSE_POLICY      = os.getenv("TIMEOUT_CLOSE_POLICY", "skip")  # skip | loss
POST_TIMEOUT_NOTICE       = os.getenv("POST_TIMEOUT_NOTICE", "0") == "1"
MAX_OPEN_WINDOW_SEC       = int(os.getenv("MAX_OPEN_WINDOW_SEC", "150"))
EXTEND_ONLY_ON_RELEVANT   = os.getenv("EXTEND_ONLY_ON_RELEVANT", "1") == "1"
CLOSE_STUCK_AFTER_SEC     = int(os.getenv("CLOSE_STUCK_AFTER_SEC", "180"))

# Exploração (quando delta é baixo)
EXPLORE_EPS    = float(os.getenv("EXPLORE_EPS", "0.35"))
EXPLORE_MARGIN = float(os.getenv("EXPLORE_MARGIN", "0.15"))
EPS_TIE        = float(os.getenv("EPS_TIE", "0.02"))
SIDE_STREAK_CAP= int(os.getenv("SIDE_STREAK_CAP", "3"))

# Rolling / tendência / janela ouro
ROLLING_MAX = int(os.getenv("ROLLING_MAX", "600"))
TREND_LOOKBACK = int(os.getenv("TREND_LOOKBACK", "40"))
DISABLE_WINDOWS = os.getenv("DISABLE_WINDOWS", "0") == "1"
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "25"))
TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.88"))
TOP_HOURS_COUNT       = int(os.getenv("TOP_HOURS_COUNT", "6"))

# Pesos p/ score base
W_WR, W_HOUR, W_TREND = 0.70, 0.20, 0.10

# Especialistas
ROLL_BAN_AFTER_R = int(os.getenv("ROLL_BAN_AFTER_R", "0"))   # 0 = off
ROLL_BAN_HOURS   = int(os.getenv("ROLL_BAN_HOURS", "2"))
MIN_SIDE_SAMPLES = int(os.getenv("MIN_SIDE_SAMPLES", "30"))

SOURCE_GALE_PRESSURE_LOOKBACK = int(os.getenv("SOURCE_GALE_PRESSURE_LOOKBACK","25"))
SOURCE_GALE_PRESSURE_MAX      = float(os.getenv("SOURCE_GALE_PRESSURE_MAX","0.5"))
SOURCE_GALE_BLOCK             = os.getenv("SOURCE_GALE_BLOCK","0") == "1"
SOURCE_GALE_EXPLORE_BONUS     = float(os.getenv("SOURCE_GALE_EXPLORE_BONUS","0.25"))

COOLDOWN_AFTER_LOSS_MIN = int(os.getenv("COOLDOWN_AFTER_LOSS_MIN","10"))

# Log / misc
FLOW_THROUGH = os.getenv("FLOW_THROUGH", "0") == "1"
LOG_RAW      = os.getenv("LOG_RAW", "1") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ================== APP ==================
app = FastAPI()
log = logging.getLogger("uvicorn.error")

# ================== REGEX ==================
SIDE_WORD_RE = re.compile(r"\b(player|banker|empate|azul|vermelho|blue|red|p\b|b\b)\b", re.I)
EMOJI_PLAYER_RE = re.compile(r"(🔵|🟦)", re.U)
EMOJI_BANKER_RE = re.compile(r"(🔴|🟥)", re.U)

# Fonte: finalização
SOURCE_G0_GREEN_RE = re.compile(
    r"(de\s+primeira|sem\s+gale|bateu\s+g0|\bG0\b|acert(ou|amos)\s+de\s+primeira)", re.I
)
SOURCE_WENT_GALE_RE = re.compile(
    r"((estamos|indo|fomos|vamos)\s+(pro|para|no)\s*g-?\s*[12]\b|gale\s*[12]\b)", re.I
)

# ================== HELPERS ==================
def now_local(): return datetime.now(LOCAL_TZ)

def colorize_side(s):
    return {"player":"🔵 Player", "banker":"🔴 Banker", "empate":"🟡 Empate"}.get(s or "", "?")

def normalize_side_token(tok:str|None)->str|None:
    if not tok: return None
    t=tok.lower()
    if t in ("player","p","azul","blue"): return "player"
    if t in ("banker","b","vermelho","red"): return "banker"
    if t == "empate": return "empate"
    return None

def extract_side(text:str)->str|None:
    if not text: return None
    m = SIDE_WORD_RE.search(text)
    if m:
        s = normalize_side_token(m.group(1))
        if s: return s
    has_p = bool(EMOJI_PLAYER_RE.search(text))
    has_b = bool(EMOJI_BANKER_RE.search(text))
    if has_p and not has_b: return "player"
    if has_b and not has_p: return "banker"
    return None

# ================== STATE ==================
STATE = {
    "pattern_roll": {},               # side -> deque("G"/"R")
    "recent_results": deque(maxlen=TREND_LOOKBACK),
    "totals": {"greens":0, "reds":0},
    "streak_green": 0,
    "open_signal": None,              # {"chosen_side","fonte_side","expires_at","src_msg_id","src_opened_epoch"}
    "last_publish_ts": None,
    "processed_updates": deque(maxlen=500),
    "messages": [],
    "source_texts": deque(maxlen=120),
    "hour_stats": {},                 # "00".."23" -> {"g","t"}
    "side_ban_until": {},             # {"player": iso, "banker": iso}
    "cooldown_until": None,
    "last_side": None,
    "last_side_streak": 0,
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

# ================== MÉTRICAS / SCORE ==================
def _bump_hour(res:str):
    hh = now_local().strftime("%H")
    h = STATE["hour_stats"].setdefault(hh, {"g":0,"t":0})
    h["t"] += 1
    if res == "G": h["g"] += 1

def rolling_append(side:str,res:str):
    dq=STATE["pattern_roll"].setdefault(side, deque(maxlen=ROLLING_MAX))
    dq.append(res)
    _bump_hour(res)

def side_wr(side:str)->tuple[float,int]:
    dq=STATE["pattern_roll"].get(side, deque())
    n=len(dq)
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
        if wr >= TOP_HOURS_MIN_WINRATE:
            top.append(h)
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
    if s_p >= s_b:
        return ("player", s_p, s_b, {"score_player":s_p, "score_banker":s_b, "delta": s_p-s_b})
    else:
        return ("banker", s_b, s_p, {"score_player":s_p, "score_banker":s_b, "delta": s_b-s_p})

def pick_by_streak_fallback():
    return "player" if random.random()<0.5 else "banker"

# ===== helpers janela / extensão =====
def _open_age_secs():
    osig = STATE.get("open_signal")
    if not osig: return 0
    try:
        opened = int(osig.get("src_opened_epoch") or 0)
        now_ep = int(datetime.now(LOCAL_TZ).timestamp())
        return max(0, now_ep - opened)
    except Exception:
        return 0

def _can_extend_ttl(text: str) -> bool:
    if not EXTEND_ONLY_ON_RELEVANT:
        return True
    low = (text or "").lower()
    return bool(
        SIDE_WORD_RE.search(low) or
        EMOJI_PLAYER_RE.search(text or "") or
        EMOJI_BANKER_RE.search(text or "") or
        SOURCE_WENT_GALE_RE.search(low) or
        SOURCE_G0_GREEN_RE.search(low)
    )

# ================== ESPECIALISTAS (ABERTURA) ==================
def rolling_tail(side:str, k:int)->str:
    dq = STATE["pattern_roll"].get(side, deque())
    return "".join(list(dq)[-k:])

def is_side_banned(side:str)->bool:
    until = (STATE.get("side_ban_until") or {}).get(side)
    return bool(until and datetime.fromisoformat(until) > now_local())

def ban_side(side:str, hrs:int, reason:str):
    STATE["side_ban_until"][side] = (now_local() + timedelta(hours=hrs)).isoformat()
    save_state()
    asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"ban_side","side":side,"reason":reason}))

def spec_roll_guard(side:str)->tuple[bool,str]:
    wr, n = side_wr(side)
    if n < MIN_SIDE_SAMPLES:
        return True, "warmup"
    if is_side_banned(side):
        return False, "side_banned"
    if ROLL_BAN_AFTER_R > 0:
        tail = rolling_tail(side, ROLL_BAN_AFTER_R)
        if tail.endswith("R"*ROLL_BAN_AFTER_R):
            ban_side(side, ROLL_BAN_HOURS, f"{ROLL_BAN_AFTER_R}R seguidos")
            return False, "tail_rr_banned"
    return True, "ok"

def spec_hour_window()->tuple[bool,str]:
    return (True,"ok") if is_top_hour_now() else (False,"off_window")

def spec_source_gale_pressure()->tuple[bool,float,str]:
    texts = list(STATE.get("source_texts", deque()))[-SOURCE_GALE_PRESSURE_LOOKBACK:]
    if not texts:
        return True, 0.0, "no_data"
    gales = sum(1 for t in texts if SOURCE_WENT_GALE_RE.search((t or "").lower()))
    ratio = gales/len(texts)
    if ratio >= SOURCE_GALE_PRESSURE_MAX:
        if SOURCE_GALE_BLOCK:
            return False, ratio, "blocked_by_pressure"
        return True, ratio, "explore_boost"
    return True, ratio, "ok"

def cooldown_active()->bool:
    cu = STATE.get("cooldown_until")
    return bool(cu and datetime.fromisoformat(cu) > now_local())

def start_cooldown():
    STATE["cooldown_until"] = (now_local() + timedelta(minutes=COOLDOWN_AFTER_LOSS_MIN)).isoformat()
    save_state()

def spec_side_repeat_guard(candidate:str)->tuple[bool,str]:
    if SIDE_STREAK_CAP <= 0:
        return True,"ok"
    if STATE.get("last_side") == candidate and STATE.get("last_side_streak",0) >= SIDE_STREAK_CAP:
        return False, "side_repeat_cap"
    return True,"ok"

# ================== RESULTADO vs FONTE (com NEUTRO) ==================
def derive_our_result_from_source_flow(text:str, fonte_side:str|None, chosen_side:str|None):
    """
    - Fonte foi para G1/G2 => igual ao fonte = LOSS ; oposto = GREEN
    - Fonte bateu G0 (de primeira/sem gale) => igual ao fonte = GREEN ; oposto = NEUTRO
    """
    if not fonte_side or not chosen_side:
        return None
    low = (text or "").lower()
    if SOURCE_WENT_GALE_RE.search(low):
        return "R" if (chosen_side == fonte_side) else "G"
    if SOURCE_G0_GREEN_RE.search(low):
        return "G" if (chosen_side == fonte_side) else "N"
    return None

# ================== PUBLICAÇÃO ==================
async def publish_entry(chosen_side:str, fonte_side:str|None, msg:dict):
    pretty = (
        "🚀 <b>ENTRADA AUTÔNOMA (G0)</b>\n"
        f"{colorize_side(chosen_side)}\n"
        f"Fonte: {colorize_side(fonte_side) if fonte_side else '?'}"
    )
    await tg_send(TARGET_CHAT_ID, pretty)
    STATE["open_signal"] = {
        "ts": now_local().isoformat(),
        "chosen_side": chosen_side,
        "fonte_side": fonte_side,
        "expires_at": (now_local()+timedelta(seconds=OPEN_TTL_SEC)).isoformat(),
        "src_msg_id": msg.get("message_id"),
        "src_opened_epoch": msg.get("date", 0),
    }
    if STATE.get("last_side") == chosen_side:
        STATE["last_side_streak"] = STATE.get("last_side_streak",0) + 1
    else:
        STATE["last_side_streak"] = 1
    STATE["last_side"] = chosen_side
    STATE["last_publish_ts"] = now_local().isoformat()
    save_state()

async def announce_outcome(result:str, chosen_side:str|None):
    if result == "G":
        big = "🟩🟩🟩 <b>GREEN</b> 🟩🟩🟩"
    elif result == "R":
        big = "🟥🟥🟥 <b>LOSS</b> 🟥🟥🟥"
    else:  # "N"
        await tg_send(TARGET_CHAT_ID, f"⚪ <b>NEUTRO</b>\n⏱ {now_local().strftime('%H:%M:%S')}\nNossa: {colorize_side(chosen_side)}")
        return
    await tg_send(TARGET_CHAT_ID, f"{big}\n⏱ {now_local().strftime('%H:%M:%S')}\nNossa: {colorize_side(chosen_side)}")
    g=STATE["totals"]["greens"]; r=STATE["totals"]["reds"]; t=g+r; wr=(g/t*100.0) if t else 0.0
    await tg_send(TARGET_CHAT_ID, f"📊 <b>Placar Geral</b>\n✅ {g}   ⛔️ {r}\n🎯 {wr:.2f}%  •  🔥 Streak {STATE['streak_green']}")

# ================== UPDATE cor do fonte ==================
def maybe_update_fonte_side_from_msg(msg:dict):
    osig = STATE.get("open_signal")
    if not osig: return
    if osig.get("fonte_side"): return
    raw = (msg.get("text") or msg.get("caption") or "")
    side = extract_side(raw)
    if side:
        osig["fonte_side"] = side
        save_state()

# ================== ABERTURA ==================
async def try_open_from_source(msg:dict):
    if OPEN_STRICT_SERIAL and STATE.get("open_signal"):
        if EXTEND_TTL_ON_ACTIVITY and _open_age_secs() < MAX_OPEN_WINDOW_SEC:
            raw = (msg.get("text") or msg.get("caption") or "")
            if _can_extend_ttl(raw):
                osig=STATE["open_signal"]
                osig["expires_at"] = (now_local()+timedelta(seconds=RESULT_GRACE_EXTEND_SEC)).isoformat()
                save_state()
        maybe_update_fonte_side_from_msg(msg)
        return

    last = STATE.get("last_publish_ts")
    if last:
        try:
            if (now_local()-datetime.fromisoformat(last)).total_seconds() < MIN_GAP_SECS:
                return
        except: pass

    if cooldown_active():
        return

    raw_text = (msg.get("text") or msg.get("caption") or "")
    fonte_side = extract_side(raw_text)

    winner, s_win, s_lose, dbg = compute_scores()
    delta = dbg["delta"]

    # especialistas
    ok1,_ = spec_roll_guard(winner)
    if not ok1: return

    ok2,_ = spec_hour_window()
    if not ok2: return

    ok3, ratio, why3 = spec_source_gale_pressure()
    if not ok3: return
    eps = EXPLORE_EPS + (SOURCE_GALE_EXPLORE_BONUS if why3 == "explore_boost" else 0.0)

    ok5,_ = spec_side_repeat_guard(winner)
    if not ok5:
        alt = "player" if winner=="banker" else "banker"
        ok_alt,_ = spec_roll_guard(alt)
        if ok_alt: winner = alt
        else: return

    # exploração
    if delta < EXPLORE_MARGIN:
        if fonte_side and random.random() < eps:
            winner = "player" if fonte_side=="banker" else "banker"
        elif not fonte_side:
            winner = pick_by_streak_fallback()

    await publish_entry(winner, fonte_side, msg)

# ================== FECHAMENTO ==================
async def maybe_close_from_source(msg:dict):
    osig = STATE.get("open_signal")
    if not osig: return
    raw_text = (msg.get("text") or msg.get("caption") or "")
    low  = raw_text.lower()

    maybe_update_fonte_side_from_msg(msg)

    has_flow_confirm = bool(SOURCE_G0_GREEN_RE.search(low) or SOURCE_WENT_GALE_RE.search(low))
    if not has_flow_confirm:
        if EXTEND_TTL_ON_ACTIVITY and _open_age_secs() < MAX_OPEN_WINDOW_SEC and _can_extend_ttl(raw_text):
            osig["expires_at"] = (now_local()+timedelta(seconds=RESULT_GRACE_EXTEND_SEC)).isoformat()
            save_state()
        return

    if ENFORCE_RESULT_AFTER_NEW_SOURCE_MESSAGE and (msg.get("message_id") == osig.get("src_msg_id")):
        return

    opened_epoch = osig.get("src_opened_epoch", 0)
    if opened_epoch and (msg.get("date", 0) - opened_epoch) < MIN_RESULT_DELAY_SEC:
        return

    our_res = derive_our_result_from_source_flow(raw_text, osig.get("fonte_side"), osig.get("chosen_side"))
    if our_res is None:
        if EXTEND_TTL_ON_ACTIVITY and _open_age_secs() < MAX_OPEN_WINDOW_SEC and _can_extend_ttl(raw_text):
            osig["expires_at"] = (now_local()+timedelta(seconds=RESULT_GRACE_EXTEND_SEC)).isoformat()
            save_state()
        return

    # aplicar placar
    if our_res == "G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] += 1
        rolling_append(osig.get("chosen_side"), "G")
    elif our_res == "R":
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0
        rolling_append(osig.get("chosen_side"), "R")
        start_cooldown()
    else:  # "N" NEUTRO — não altera contadores/streak
        pass

    await announce_outcome(our_res, osig.get("chosen_side"))
    STATE["open_signal"] = None
    save_state()

# ================== TIMEOUT / ANTI-TRAVA ==================
async def expire_open_if_needed():
    osig = STATE.get("open_signal")
    if not osig: return

    if _open_age_secs() >= CLOSE_STUCK_AFTER_SEC:
        STATE["open_signal"] = None
        save_state()
        return

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

# ================== FASTAPI ==================
@app.middleware("http")
async def safe_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        logging.exception("Middleware error: %s", e)
        return JSONResponse({"ok": True}, status_code=200)

@app.get("/")
async def root():
    return {"ok": True, "service": "G0 serial, fechamento rápido pelo fonte, especialistas e NEUTRO correto"}

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
    if chat.get("id") != SOURCE_CHAT_ID:
        return JSONResponse({"ok": True})

    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return JSONResponse({"ok": True})

    # histórico do fonte (pressão gale)
    STATE["source_texts"].append(text)
    save_state()

    if FLOW_THROUGH:
        await tg_send(TARGET_CHAT_ID, text)

    low = text.lower()
    has_trigger = bool(("entrada confirmada" in low) or SIDE_WORD_RE.search(low) or EMOJI_PLAYER_RE.search(text) or EMOJI_BANKER_RE.search(text))

    # 1) fechar (primeiro desfecho do fonte)
    if CLOSE_ONLY_ON_FLOW_CONFIRM:
        await maybe_close_from_source(msg)

    # 2) abrir (somente se não há aberto e houve gatilho)
    if (FORCE_TRIGGER_OPEN or FORCE_OPEN_ON_ANY_SOURCE_MSG) and has_trigger:
        await try_open_from_source(msg)

    # 3) TTL / anti-trava
    await expire_open_if_needed()
    return JSONResponse({"ok": True})