# app.py — G0 serial + fecha no 1º desfecho do fonte (G0 ou Gale) + placar pós-fechamento
import os, re, json, asyncio, pytz, logging, random, hashlib
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

# Fechamento (parâmetros mantidos por compatibilidade; não bloqueiam o fechamento imediato)
CLOSE_ONLY_ON_FLOW_CONFIRM = os.getenv("CLOSE_ONLY_ON_FLOW_CONFIRM", "1") == "1"
ENFORCE_RESULT_AFTER_NEW_SOURCE_MESSAGE = os.getenv("ENFORCE_RESULT_AFTER_NEW_SOURCE_MESSAGE", "0") == "1"
MIN_RESULT_DELAY_SEC = int(os.getenv("MIN_RESULT_DELAY_SEC", "0"))

# Timeout
OPEN_TTL_SEC              = int(os.getenv("OPEN_TTL_SEC", "90"))
EXTEND_TTL_ON_ACTIVITY    = os.getenv("EXTEND_TTL_ON_ACTIVITY", "0") == "1"
RESULT_GRACE_EXTEND_SEC   = int(os.getenv("RESULT_GRACE_EXTEND_SEC", "0"))
TIMEOUT_CLOSE_POLICY      = os.getenv("TIMEOUT_CLOSE_POLICY", "skip")  # skip | loss
POST_TIMEOUT_NOTICE       = os.getenv("POST_TIMEOUT_NOTICE", "0") == "1"

# Exploração (quando delta é baixo)
EXPLORE_EPS    = float(os.getenv("EXPLORE_EPS", "0.35"))
EXPLORE_MARGIN = float(os.getenv("EXPLORE_MARGIN", "0.15"))
EPS_TIE        = float(os.getenv("EPS_TIE", "0.02"))
SIDE_STREAK_CAP= int(os.getenv("SIDE_STREAK_CAP", "3"))

# Métricas/Janela (simples)
ROLLING_MAX = int(os.getenv("ROLLING_MAX", "600"))
TREND_LOOKBACK = int(os.getenv("TREND_LOOKBACK", "40"))

# Pesos de score
W_WR, W_HOUR, W_TREND = 0.70, 0.20, 0.10

# Log / misc
FLOW_THROUGH = os.getenv("FLOW_THROUGH", "0") == "1"
LOG_RAW      = os.getenv("LOG_RAW", "1") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ================== APP ==================
app = FastAPI()
log = logging.getLogger("uvicorn.error")

# ================== REGEX ==================
SIDE_RE = re.compile(r"\b(player|banker|empate|azul|vermelho|blue|red|p\b|b\b)\b", re.I)

# fonte finalizou G0 de primeira (sem gale)
SOURCE_G0_GREEN_RE = re.compile(
    r"(de\s+primeira|sem\s+gale|bateu\s+g0|\bG0\b|acert(ou|amos)\s+de\s+primeira)", re.I
)
# fonte foi para G1/G2 (formas comuns: “gale 1”, “gale 2”)
SOURCE_WENT_GALE_RE = re.compile(
    r"(gale\s*1\b|gale\s*2\b|indo\s+pro\s+g-?\s*[12]\b|vamos\s+pro\s+g-?\s*[12]\b|fomos\s+pro\s+g-?\s*[12]\b)", re.I
)

# ================== HELPERS ==================
def now_local(): return datetime.now(LOCAL_TZ)
def colorize_side(s):
    return {"player":"🔵 Player", "banker":"🔴 Banker", "empate":"🟡 Empate"}.get(s or "", "?")

def normalize_side(token:str|None)->str|None:
    if not token: return None
    t=token.lower()
    if t in ("player","p","azul","blue"): return "player"
    if t in ("banker","b","vermelho","red"): return "banker"
    if t == "empate": return "empate"
    return None

def extract_side(text:str)->str|None:
    m = SIDE_RE.search(text or "")
    return normalize_side(m.group(1)) if m else None

# ================== STATE ==================
STATE = {
    "pattern_roll": {},               # side -> deque("G"/"R")
    "recent_results": deque(maxlen=TREND_LOOKBACK),
    "totals": {"greens":0, "reds":0},
    "streak_green": 0,
    "open_signal": None,              # {"ts","chosen_side","fonte_side","expires_at","src_msg_id","src_opened_epoch"}
    "last_publish_ts": None,
    "processed_updates": deque(maxlen=500),
    "messages": [],
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
def rolling_append(side:str,res:str):
    dq=STATE["pattern_roll"].setdefault(side, deque(maxlen=ROLLING_MAX))
    dq.append(res)

def side_wr(side:str)->tuple[float,int]:
    dq=STATE["pattern_roll"].get(side, deque())
    n=len(dq)
    if n==0: return 0.0,0
    return (sum(1 for x in dq if x=="G")/n), n

def is_top_hour_now()->bool:
    # neutro por simplicidade
    return True

def hour_bonus()->float: return 1.0 if is_top_hour_now() else 0.0
def trend_bonus()->float:
    dq=list(STATE["recent_results"]); n=len(dq)
    if n==0: return 0.0
    wr=sum(1 for x in dq if x=="G")/n
    return max(0.0,min(1.0,wr))

def compute_scores():
    wr_p,n_p=side_wr("player"); wr_b,n_b=side_wr("banker")
    hb, tb = hour_bonus(), trend_bonus()
    # ruído minúsculo anti-empate
    s_p = max(0.0, min(1.0, (W_WR*wr_p)+(W_HOUR*hb)+(W_TREND*tb) + random.uniform(0,EPS_TIE)))
    s_b = max(0.0, min(1.0, (W_WR*wr_b)+(W_HOUR*hb)+(W_TREND*tb) + random.uniform(0,EPS_TIE)))
    if s_p >= s_b:
        return ("player", s_p, s_b, {"score_player":s_p, "score_banker":s_b, "delta": s_p-s_b})
    else:
        return ("banker", s_b, s_p, {"score_player":s_p, "score_banker":s_b, "delta": s_b-s_p})

def pick_by_streak_fallback():
    # desempate simples
    return "player" if random.random()<0.5 else "banker"

# ================== RESULTADO VIA FLUXO DO FONTE ==================
def derive_our_result_from_source_flow(text:str, fonte_side:str|None, chosen_side:str|None):
    """
    Regras:
    - Se fonte disse 'de primeira/sem gale/G0' → nossa cor = fonte ? G : R
    - Se fonte disse 'gale 1/2' → nossa cor = fonte ? R : G
    """
    if not fonte_side or not chosen_side:
        return None
    low = (text or "").lower()
    if SOURCE_G0_GREEN_RE.search(low):
        return "G" if (chosen_side == fonte_side) else "R"
    if SOURCE_WENT_GALE_RE.search(low):
        return "R" if (chosen_side == fonte_side) else "G"
    return None

def maybe_update_fonte_side_from_msg(msg: dict):
    """Se o fonte mencionou a cor nesta mensagem, atualiza no open_signal."""
    osig = STATE.get("open_signal")
    if not osig:
        return
    txt = (msg.get("text") or msg.get("caption") or "")
    side = extract_side(txt)
    if side in ("player","banker","empate"):
        osig["fonte_side"] = side
        save_state()

# ================== PUBLICAÇÃO ==================
async def publish_entry(chosen_side:str, fonte_side:str|None, msg_id:int, msg_epoch:int):
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
        "src_msg_id": msg_id,
        "src_opened_epoch": msg_epoch,
    }
    STATE["last_publish_ts"] = now_local().isoformat()
    save_state()

async def announce_outcome(result:str, chosen_side:str|None):
    big = "🟩🟩🟩 <b>GREEN</b> 🟩🟩🟩" if result=="G" else "🟥🟥🟥 <b>LOSS</b> 🟥🟥🟥"
    await tg_send(TARGET_CHAT_ID, f"{big}\n⏱ {now_local().strftime('%H:%M:%S')}\nNossa: {colorize_side(chosen_side)}")
    g=STATE["totals"]["greens"]; r=STATE["totals"]["reds"]; t=g+r; wr=(g/t*100.0) if t else 0.0
    await tg_send(TARGET_CHAT_ID, f"📊 <b>Placar Geral</b>\n✅ {g}   ⛔️ {r}\n🎯 {wr:.2f}%  •  🔥 Streak {STATE['streak_green']}")

# ================== ABERTURA ==================
async def try_open_from_source(msg:dict):
    if OPEN_STRICT_SERIAL and STATE.get("open_signal"):
        # já tem aberto (não abre outro em paralelo)
        if EXTEND_TTL_ON_ACTIVITY:
            osig=STATE["open_signal"]
            osig["expires_at"] = (now_local()+timedelta(seconds=RESULT_GRACE_EXTEND_SEC)).isoformat()
            save_state()
        return

    # anti-spam mínimo entre aberturas
    last = STATE.get("last_publish_ts")
    if last:
        try:
            if (now_local()-datetime.fromisoformat(last)).total_seconds() < MIN_GAP_SECS:
                return
        except: pass

    fonte_side = extract_side(msg.get("text",""))
    # decide lado
    winner, s_win, s_lose, dbg = compute_scores()
    delta = dbg["delta"]
    if delta < EXPLORE_MARGIN:
        if fonte_side and random.random() < EXPLORE_EPS:
            winner = "player" if fonte_side=="banker" else "banker"
        elif not fonte_side:
            winner = pick_by_streak_fallback()

    await publish_entry(
        chosen_side=winner,
        fonte_side=fonte_side,
        msg_id=msg.get("message_id"),
        msg_epoch=msg.get("date", 0)
    )

# ================== FECHAMENTO (IMEDIATO NO 1º DESFECHO) ==================
async def maybe_close_from_source(msg:dict):
    osig = STATE.get("open_signal")
    if not osig: return

    raw_text = (msg.get("text") or msg.get("caption") or "")
    low = raw_text.lower()

    # atualizar cor do fonte, se estiver citada nesta msg
    maybe_update_fonte_side_from_msg(msg)

    # checar “G0 de primeira” OU “GALE 1/2”
    has_g0   = bool(SOURCE_G0_GREEN_RE.search(low))
    has_gale = bool(SOURCE_WENT_GALE_RE.search(low))
    if not (has_g0 or has_gale):
        return  # nada a fechar ainda

    # fechamento imediato (sem checar “nova msg” ou “delay”)
    our_res = derive_our_result_from_source_flow(
        raw_text,
        osig.get("fonte_side"),
        osig.get("chosen_side")
    )
    if our_res is None:
        return  # faltou cor explícita do fonte; esperar próxima msg

    # aplicar placar
    if our_res == "G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] += 1
        rolling_append(osig.get("chosen_side"), "G")
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0
        rolling_append(osig.get("chosen_side"), "R")

    await announce_outcome(our_res, osig.get("chosen_side"))
    STATE["open_signal"] = None
    save_state()

# ================== TIMEOUT ==================
async def expire_open_if_needed():
    osig = STATE.get("open_signal")
    if not osig: return
    exp = osig.get("expires_at")
    if not exp: return
    if datetime.fromisoformat(exp) > now_local(): return

    # expirou
    STATE["open_signal"] = None
    save_state()
    if TIMEOUT_CLOSE_POLICY == "loss":
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0
        await announce_outcome("R", None)
    elif POST_TIMEOUT_NOTICE:
        await tg_send(TARGET_CHAT_ID, "⏳ Encerrado por timeout (TTL) — descartado")

# ================== FASTAPI ==================
@app.middleware("http")
async def safe_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        log.exception("Middleware error: %s", e)
        return JSONResponse({"ok": True}, status_code=200)

@app.get("/")
async def root():
    return {"ok": True, "service": "G0 serial; fecha no 1º desfecho do fonte (G0 ou Gale)"}

@app.post("/webhook")
@app.post("/webhook/{secret}")
async def webhook(req: Request, secret: str|None=None):
    try:
        update = await req.json()
    except Exception:
        return JSONResponse({"ok": True})

    if LOG_RAW: log.info("RAW UPDATE: %s", update)

    # dedup por update_id
    up_id = update.get("update_id")
    if up_id is not None:
        if up_id in STATE["processed_updates"]: return JSONResponse({"ok": True})
        STATE["processed_updates"].append(up_id)

    msg = update.get("channel_post") or update.get("message") or {}
    chat = (msg.get("chat") or {})
    if chat.get("id") != SOURCE_CHAT_ID:
        return JSONResponse({"ok": True})

    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text: return JSONResponse({"ok": True})

    # 1) tentar FECHAR primeiro (prioridade para não acumular aberturas)
    await maybe_close_from_source(msg)

    # 2) abrir (gatilho) — só se não tem aberto
    if FORCE_TRIGGER_OPEN or FORCE_OPEN_ON_ANY_SOURCE_MSG:
        await try_open_from_source(msg)

    # 3) tratar TTL
    await expire_open_if_needed()
    return JSONResponse({"ok": True})