# app.py — G0 AUTÔNOMO • Serial 1-por-vez • Fecha SÓ com confirmação do fluxo (G0 do fonte OU Gale) • TTL com graça
import os, json, asyncio, re, pytz, hashlib, random
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx, logging
from collections import deque

# ================== ENV ==================
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])   # canal-fonte
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])   # canal-destino

TZ_NAME        = os.getenv("TZ", "UTC")
LOCAL_TZ       = pytz.timezone(TZ_NAME)

# Rolling / filtros (internos)
MIN_G0 = float(os.getenv("MIN_G0", "0.95"))
MIN_SAMPLES_BEFORE_FILTER = int(os.getenv("MIN_SAMPLES_BEFORE_FILTER", "40"))
ROLLING_MAX = int(os.getenv("ROLLING_MAX", "800"))
TREND_LOOKBACK = int(os.getenv("TREND_LOOKBACK", "40"))
TREND_MIN_WR   = float(os.getenv("TREND_MIN_WR", "0.80"))

# Janela de ouro (apenas etiqueta)
TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.85"))
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "20"))
TOP_HOURS_COUNT       = int(os.getenv("TOP_HOURS_COUNT", "8"))

# Ban/risco
BAN_AFTER_CONSECUTIVE_R = int(os.getenv("BAN_AFTER_CONSECUTIVE_R", "1"))
BAN_FOR_HOURS           = int(os.getenv("BAN_FOR_HOURS", "4"))
DAILY_STOP_LOSS         = int(os.getenv("DAILY_STOP_LOSS", "999"))
STREAK_GUARD_LOSSES     = int(os.getenv("STREAK_GUARD_LOSSES", "0"))
COOLDOWN_MINUTES        = int(os.getenv("COOLDOWN_MINUTES", "15"))
HOURLY_CAP              = int(os.getenv("HOURLY_CAP", "20"))

# Fluxo
MIN_GAP_SECS = int(os.getenv("MIN_GAP_SECS", "5"))
FLOW_THROUGH = os.getenv("FLOW_THROUGH", "0") == "1"
LOG_RAW      = os.getenv("LOG_RAW", "1") == "1"
DISABLE_WINDOWS = os.getenv("DISABLE_WINDOWS", "0") == "1"
DISABLE_RISK    = os.getenv("DISABLE_RISK", "1") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ===== Decisão/autonomia =====
FORCE_TRIGGER_OPEN            = os.getenv("FORCE_TRIGGER_OPEN", "1") == "1"   # abre no gatilho do fonte
FORCE_OPEN_ON_ANY_SOURCE_MSG  = os.getenv("FORCE_OPEN_ON_ANY_SOURCE_MSG", "1") == "1"  # abre em qualquer msg do fonte
AUTO_MIN_SIDE_SAMPLES         = int(os.getenv("AUTO_MIN_SIDE_SAMPLES", "0"))
AUTO_MIN_SCORE                = float(os.getenv("AUTO_MIN_SCORE", "0.50"))
AUTO_MIN_MARGIN               = float(os.getenv("AUTO_MIN_MARGIN", "0.05"))

# Pesos do score
W_WR, W_HOUR, W_TREND = 0.70, 0.20, 0.10

# Anti-vício (exploração / tie / streak)
EXPLORE_MARGIN = float(os.getenv("EXPLORE_MARGIN", "0.15"))
EXPLORE_EPS    = float(os.getenv("EXPLORE_EPS", "0.35"))
SIDE_STREAK_CAP= int(os.getenv("SIDE_STREAK_CAP", "3"))
EPS_TIE        = float(os.getenv("EPS_TIE", "0.02"))

# ===== Fonte desacoplada / placar
LEARN_FROM_SOURCE_RESULTS = os.getenv("LEARN_FROM_SOURCE_RESULTS", "0") == "1"  # 0 = NÃO alimentar modelo
SCOREBOARD_FROM_SOURCE    = os.getenv("SCOREBOARD_FROM_SOURCE", "1") == "1"     # 1 = atualiza placar

# ===== Fechar SÓ com confirmação do fluxo do fonte ====
CLOSE_ONLY_ON_FLOW_CONFIRM = os.getenv("CLOSE_ONLY_ON_FLOW_CONFIRM", "1") == "1"

# ===== Delay e "outra mensagem" ====
ONLY_ACCEPT_RESULT_IF_OPEN = os.getenv("ONLY_ACCEPT_RESULT_IF_OPEN", "1") == "1"
MIN_RESULT_DELAY_SEC       = int(os.getenv("MIN_RESULT_DELAY_SEC", "8"))
ENFORCE_RESULT_AFTER_NEW_SOURCE_MESSAGE = os.getenv("ENFORCE_RESULT_AFTER_NEW_SOURCE_MESSAGE", "1") == "1"

# ===== Serial (1 por vez) + TTL com graça ====
OPEN_STRICT_SERIAL = os.getenv("OPEN_STRICT_SERIAL", "1") == "1"
OPEN_TTL_SEC       = int(os.getenv("OPEN_TTL_SEC", "90"))         # TTL mais longo
TIMEOUT_CLOSE_POLICY = os.getenv("TIMEOUT_CLOSE_POLICY", "skip").lower()  # "skip" | "loss"
POST_TIMEOUT_NOTICE = os.getenv("POST_TIMEOUT_NOTICE", "0") == "1"        # 0 = silencioso por padrão

# Prorrogar TTL quando houver atividade mas SEM confirmação final
EXTEND_TTL_ON_ACTIVITY = os.getenv("EXTEND_TTL_ON_ACTIVITY", "1") == "1"
RESULT_GRACE_EXTEND_SEC = int(os.getenv("RESULT_GRACE_EXTEND_SEC", "25"))

# Visual do sinal
SHOW_DEBUG_ON_ENTRY = os.getenv("SHOW_DEBUG_ON_ENTRY", "1") == "1"

# ================== APP/LOG ==================
app = FastAPI()
log = logging.getLogger("uvicorn.error")

async def tg_send(chat_id: int, text: str, disable_preview=True):
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            await cli.post(f"{API}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": disable_preview})
    except Exception as e:
        log.error("tg_send error: %s", e)

@app.middleware("http")
async def safe_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        log.exception("Middleware error: %s", e)
        return JSONResponse({"ok": True}, status_code=200)

# ================== REGEX / PARSE ==================
SIDE_ALIASES_RE = re.compile(r"\b(player|banker|empate|p\b|b\b|azul|vermelho|blue|red)\b", re.I)
NOISE_RE   = re.compile(r"(bot\s*online|aposta\s*encerrada|analisando)", re.I)

# Confirmações explícitas do fluxo do canal-fonte
SOURCE_G0_GREEN_RE = re.compile(r"(de\s+primeira|sem\s+gale|bateu\s+g0|\bg0\b|\bacert(o|amos)\s+de\s+primeira)", re.I)
SOURCE_WENT_GALE_RE = re.compile(r"(estamos|indo|fomos|vamos)\s+(pro|para|no)\s*g-?([12])\b|gale\s*[12]\b", re.I)

# (Mantemos estes para fallback e contagens simples, mas NÃO fecham sozinhos)
GREEN_RE = re.compile(r"\b(green|win|vit[oó]ria)\b|🟩|🟢|✅", re.I)
RED_RE   = re.compile(r"\b(red|loss|derrota|perd(emos|eu|a)?|lose)\b|🟥|🔴|❌", re.I)

# Hints já existentes
G0_GREEN_HINT_RE = re.compile(r"(de\s+primeira|sem\s+gale|bateu\s+g0|\bg0\b)", re.I)
PROGRESS_GALE_RE = re.compile(
    r"(indo\s+pro\s*g[12]|vamos\s+pro\s*g[12]|fomos\s+pro\s*g[12]|"
    r"bateu\s+no\s*g[12]|entrou\s+no\s*g[12]|primeiro\s+gale|segundo\s+gale|gale\s*[12]\b)",
    re.I
)

def now_local(): return datetime.now(LOCAL_TZ)
def today_str(): return now_local().strftime("%Y-%m-%d")
def hour_str(dt=None): return (dt or now_local()).strftime("%H")
def hour_key(dt=None): return (dt or now_local()).strftime("%Y-%m-%d %H")

def colorize_line(text: str) -> str:
    t = text
    t = re.sub(r"\b(player|p|azul|blue)\b",  "🔵 Player", t, flags=re.I)
    t = re.sub(r"\b(banker|b|vermelho|red)\b","🔴 Banker", t, flags=re.I)
    t = re.sub(r"\bempate\b",                "🟡 Empate", t, flags=re.I)
    return t

def normalize_side_token(tok: str|None) -> str|None:
    if not tok: return None
    tok = tok.lower()
    if tok in ("player","p","azul","blue"): return "player"
    if tok in ("banker","b","vermelho","red"): return "banker"
    if tok == "empate": return "empate"
    return None

# ================== STATE ==================
STATE = {
    "messages": [], "last_reset_date": None,
    "pattern_roll": {}, "recent_results": deque(maxlen=TREND_LOOKBACK),
    "hour_stats": {}, "pattern_ban": {},
    "daily_losses": 0, "hourly_entries": {}, "cooldown_until": None,
    "recent_g0": [],
    "open_signal": None,  # {"ts","chosen_side","opened_epoch","expires_at","text","fonte_side"}
    "totals": {"greens": 0, "reds": 0}, "streak_green": 0,
    "last_publish_ts": None, "processed_updates": deque(maxlen=500),
    "last_summary_hash": None,
    "side_history": deque(maxlen=20),
    "last_tiebreak": "banker",
    "last_raw_text": "",
    # Controle da mensagem de abertura (para não fechar na mesma)
    "last_open_src_msg_id": None,
    "last_open_ts_epoch": None,
}

def _ensure_dir(path): d=os.path.dirname(path);  (d and not os.path.exists(d)) and os.makedirs(d, exist_ok=True)
def _jsonable(o):
    from collections import deque as _dq
    if isinstance(o,_dq): return list(o)
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
        pr=data.get("pattern_roll") or {}
        for k,v in list(pr.items()):
            try: pr[k]=deque(v,maxlen=ROLLING_MAX)
            except: pr[k]=deque(maxlen=ROLLING_MAX)
        data["pattern_roll"]=pr
        data["recent_results"]=deque(data.get("recent_results") or [], maxlen=TREND_LOOKBACK)
        data["recent_g0"]=list(data.get("recent_g0") or [])[-10:]
        data.setdefault("hourly_entries", {}); data.setdefault("pattern_ban", {})
        data.setdefault("messages", []); data.setdefault("totals", {"greens":0,"reds":0})
        if "side_history" not in data: data["side_history"]=deque(maxlen=20)
        else: data["side_history"]=deque(data["side_history"], maxlen=20)
        data.setdefault("last_tiebreak","banker")
        data.setdefault("last_raw_text","")
        data.setdefault("last_open_src_msg_id", None)
        data.setdefault("last_open_ts_epoch", None)
        STATE=data
    except Exception:
        save_state()
load_state()

# ================== MÉTRICAS / REGRAS ==================
def rolling_append(side:str,res:str):
    dq=STATE["pattern_roll"].setdefault(side,deque(maxlen=ROLLING_MAX)); dq.append(res)
    h=hour_str(); hst=STATE["hour_stats"].setdefault(h,{"g":0,"t":0}); hst["t"]+=1; (res=="G") and (hst.__setitem__("g", hst["g"]+1))

def side_wr(side: str, alpha: int = 8, beta: int = 8) -> tuple[float, int]:
    dq = STATE["pattern_roll"].get(side, deque())
    n = len(dq); g = sum(1 for x in dq if x == "G")
    wr_smooth = (g + alpha) / (n + alpha + beta) if (n + alpha + beta) > 0 else 0.5
    return wr_smooth, n

def is_top_hour_now()->bool:
    if DISABLE_WINDOWS: return True
    stats=STATE.get("hour_stats",{}); items=[(h,s) for h,s in stats.items() if s["t"]>=TOP_HOURS_MIN_SAMPLES]
    if not items: return True
    ranked=sorted(items,key=lambda kv:(kv[1]["g"]/kv[1]["t"]),reverse=True)
    top=[]
    for h,s in ranked:
        wr=s["g"]/s["t"]
        if wr>=TOP_HOURS_MIN_WINRATE: top.append(h)
        if len(top)>=TOP_HOURS_COUNT: break
    return hour_str() in top if top else True

def risk_allows()->tuple[bool,str]:
    if DISABLE_RISK: return True,""
    if STATE["daily_losses"]>=DAILY_STOP_LOSS: return False,"stop_daily"
    cu=STATE.get("cooldown_until")
    if cu and datetime.fromisoformat(cu)>now_local(): return False,"cooldown"
    return True,""

def hour_bonus(): return 1.0 if is_top_hour_now() else 0.0
def trend_bonus():
    dq=list(STATE["recent_results"]); n=len(dq)
    if n==0: return 0.0
    wr=sum(1 for x in dq if x=="G")/n; return max(0.0,min(1.0,wr))

def clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x

def compute_scores():
    wr_p,n_p=side_wr("player"); wr_b,n_b=side_wr("banker")
    hb, tb = hour_bonus(), trend_bonus()
    s_p = clamp01((W_WR*wr_p) + (W_HOUR*hb) + (W_TREND*tb))
    s_b = clamp01((W_WR*wr_b) + (W_HOUR*hb) + (W_TREND*tb))
    winner, s_win, s_lose = ("player",s_p,s_b) if s_p>=s_b else ("banker",s_b,s_p)
    dbg = {"score_player":s_p,"score_banker":s_b,"delta":abs(s_p-s_b), "n_p":n_p, "n_b":n_b}
    if abs(s_p - s_b) <= EPS_TIE:
        last = STATE.get("last_tiebreak","banker")
        winner = "player" if last == "banker" else "banker"
        s_win  = s_p if winner == "player" else s_b
        STATE["last_tiebreak"] = winner
    return winner, s_win, s_lose, dbg

def register_hourly_entry():
    k = hour_key()
    STATE["hourly_entries"][k] = STATE["hourly_entries"].get(k, 0) + 1
    save_state()

# ================== ANÚNCIOS ==================
def _score_snapshot():
    g = STATE["totals"].get("greens", 0)
    r = STATE["totals"].get("reds", 0)
    t = g + r
    wr = (g / t * 100.0) if t else 0.0
    streak = STATE.get("streak_green", 0)
    return g, r, wr, streak

async def announce_outcome(result: str, chosen_side: str | None):
    big = "🟩🟩🟩 <b>GREEN</b> 🟩🟩🟩" if result == "G" else "🟥🟥🟥 <b>LOSS</b> 🟥🟥🟥"
    ns = {"player":"🔵 Player","banker":"🔴 Banker","empate":"🟡 Empate"}.get((chosen_side or "").lower(),"?")
    ts = now_local().strftime("⏱ %H:%M:%S")
    await tg_send(TARGET_CHAT_ID, f"{big}\n{ts}\nNossa: {ns}")
    g, r, wr, streak = _score_snapshot()
    await tg_send(TARGET_CHAT_ID, f"📊 <b>Placar Geral</b>\n✅ {g}   ⛔️ {r}\n🎯 {wr:.2f}%  •  🔥 Streak {streak}")

# ================== TIMEOUT/HELPERS ==================
def _is_open_expired() -> bool:
    osig = STATE.get("open_signal")
    if not osig: return False
    try:
        exp = osig.get("expires_at")
        return bool(exp) and (datetime.fromisoformat(exp) <= now_local())
    except Exception:
        return True

async def _timeout_close_open_signal(reason: str = "timeout"):
    osig = STATE.get("open_signal")
    if not osig:
        return
    chosen = osig.get("chosen_side")

    if POST_TIMEOUT_NOTICE:
        side_txt = {"player":"🔵 Player","banker":"🔴 Banker","empate":"🟡 Empate"}.get((chosen or "").lower(),"?")
        await tg_send(TARGET_CHAT_ID, f"⏳ <i>Encerrado por {reason}</i>\nNossa: {side_txt}")

    if TIMEOUT_CLOSE_POLICY == "loss":
        await announce_outcome("R", chosen)
        if SCOREBOARD_FROM_SOURCE:
            STATE["totals"]["reds"] = STATE["totals"].get("reds", 0) + 1
            STATE["streak_green"] = 0

    STATE["open_signal"] = None
    save_state()

async def _check_and_timeout_open():
    if _is_open_expired():
        await _timeout_close_open_signal("timeout (TTL)")

# ================== ABERTURA AUTÔNOMA ==================
async def publish_entry(final_side:str, dbg_line:str, window_tag:str):
    lines = [f"🚀 <b>ENTRADA AUTÔNOMA (G0)</b>{window_tag}",
             "🔵 Player" if final_side=='player' else "🔴 Banker"]
    if SHOW_DEBUG_ON_ENTRY and dbg_line:
        lines.append(dbg_line)
    await tg_send(TARGET_CHAT_ID, "\n".join(lines))
    register_hourly_entry()

async def autonomous_open_from_trigger():
    allow_risk,_ = risk_allows()
    if not allow_risk and not FORCE_TRIGGER_OPEN:
        return

    last = STATE.get("last_publish_ts")
    if last:
        try:
            if (now_local() - datetime.fromisoformat(last)).total_seconds() < MIN_GAP_SECS:
                return
        except: pass

    # Serial: se já tem aberto, não abre outro
    if OPEN_STRICT_SERIAL and STATE.get("open_signal"):
        return

    winner, s_win, s_lose, dbg = compute_scores()
    delta = dbg["delta"]

    if delta < EXPLORE_MARGIN:
        underdog = "banker" if winner == "player" else "player"
        if random.random() < EXPLORE_EPS:
            winner, s_win = underdog, (dbg["score_banker"] if underdog == "banker" else dbg["score_player"])

    hist = STATE.get("side_history")
    if isinstance(hist, deque) and len(hist) >= SIDE_STREAK_CAP and all(x == hist[-1] for x in list(hist)[-SIDE_STREAK_CAP:]):
        winner = "banker" if hist[-1] == "player" else "player"

    if not FORCE_TRIGGER_OPEN:
        if min(dbg["n_p"], dbg["n_b"]) < AUTO_MIN_SIDE_SAMPLES: return
        if s_win < AUTO_MIN_SCORE: return
        if delta < AUTO_MIN_MARGIN: return

    window_tag = "" if is_top_hour_now() else " <i>(fora da janela de ouro)</i>"

    # lado do fonte (para a regra de fechamento)
    fonte_side = None
    m = SIDE_ALIASES_RE.search(STATE.get("last_raw_text") or "")
    if m: fonte_side = normalize_side_token(m.group(1))

    dbg_line = (
        f"Fonte: {fonte_side or '?'}  |  Escolha: {winner}  |  "
        f"Score P={dbg['score_player']:.2f} • Score B={dbg['score_banker']:.2f} • Δ={delta:.2f}"
    )

    now = now_local()
    STATE["open_signal"] = {
        "ts": now.isoformat(),
        "opened_epoch": int(now.timestamp()),
        "chosen_side": winner,
        "expires_at": (now + timedelta(seconds=OPEN_TTL_SEC)).isoformat(),
        "text": dbg_line,
        "fonte_side": fonte_side,
    }
    STATE["last_publish_ts"] = now.isoformat()
    if isinstance(hist, deque): hist.append(winner)
    save_state()

    await publish_entry(winner, dbg_line, window_tag)

# ================== PROCESSAMENTO (fechar só com confirmação do fluxo) ==================
def derive_our_result_from_source_flow(text: str, fonte_side: str|None, chosen_side: str|None) -> str|None:
    """
    - Fonte confirma G0 de primeira -> se jogamos OPOSTO do fonte: LOSS ; se IGUAL: GREEN.
    - Fonte confirma G1/G2 (gale)   -> fonte errou o G0 -> se OPOSTO: GREEN ; se IGUAL: LOSS.
    - Ambíguo -> None (não fecha).
    """
    if not text or not fonte_side or not chosen_side:
        return None
    low = text.lower()

    # G0 de primeira do fonte
    if SOURCE_G0_GREEN_RE.search(low) or G0_GREEN_HINT_RE.search(low):
        return "G" if (chosen_side == fonte_side) else "R"

    # Fonte entrou em Gale (G1/G2)
    if SOURCE_WENT_GALE_RE.search(low) or PROGRESS_GALE_RE.search(low):
        return "R" if (chosen_side == fonte_side) else "G"

    return None

async def aux_log(e:dict):
    STATE["messages"].append(e)
    if len(STATE["messages"])>5000: STATE["messages"]=STATE["messages"][-4000:]
    save_state()

async def process_signal(text: str):
    low = (text or "").lower()
    if NOISE_RE.search(low):
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"noise","text":text}))
        return
    await autonomous_open_from_trigger()

# ================== RELATÓRIO ==================
def _score_snapshot():
    g = STATE["totals"].get("greens", 0)
    r = STATE["totals"].get("reds", 0)
    t = g + r
    wr = (g / t * 100.0) if t else 0.0
    streak = STATE.get("streak_green", 0)
    return g, r, wr, streak

def build_report():
    g,r=STATE["totals"]["greens"],STATE["totals"]["reds"]; t=g+r; wr=(g/t*100.0) if t else 0.0
    return f"<b>📊 Relatório Diário ({today_str()})</b>\nDia: <b>{g}G / {r}R</b> (WR: {wr:.1f}%)"

async def daily_report_loop():
    await asyncio.sleep(5)
    while True:
        try:
            n = now_local()
            if n.hour==0 and n.minute==0:
                STATE["daily_losses"]=0; STATE["hourly_entries"]={}; STATE["streak_green"]=0
                await tg_send(TARGET_CHAT_ID, build_report())
                await asyncio.sleep(65)
            else:
                await asyncio.sleep(10)
        except Exception:
            await asyncio.sleep(5)

async def housekeeping_loop():
    await asyncio.sleep(2)
    while True:
        try:
            # fecha por TTL se expirado
            await _check_and_timeout_open()
            await asyncio.sleep(2)
        except Exception:
            await asyncio.sleep(2)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(daily_report_loop())
    asyncio.create_task(housekeeping_loop())

# ================== ROTAS ==================
@app.get("/")
async def root():
    return {"ok": True, "service": "G0 autônomo • Serial • Fecha só com confirmação do fluxo (G0/Gale) • TTL com graça"}

@app.post("/webhook")
@app.post("/webhook/{secret}")
async def webhook(req: Request, secret: str|None=None):
    # tolerante a payload estranho
    try:
        update = await req.json()
    except Exception:
        body = (await req.body())[:1000]
        if LOG_RAW: log.info("RAW(non-json): %r", body)
        return JSONResponse({"ok": True})

    if LOG_RAW: log.info("RAW UPDATE: %s", update)

    # dedup
    up_id = update.get("update_id")
    if up_id is not None:
        if up_id in STATE["processed_updates"]: return JSONResponse({"ok": True})
        STATE["processed_updates"].append(up_id)

    msg = update.get("channel_post") or update.get("message") or {}
    chat = (msg.get("chat") or {})
    text = (msg.get("text") or "").strip()
    if not text:
        text = (msg.get("caption") or "").strip()

    msg_id = msg.get("message_id")
    msg_date_epoch = msg.get("date")  # epoch (segundos)

    # 0) housekeeping e TTL
    await _check_and_timeout_open()

    # ===== comandos no canal destino (opcional) =====
    if chat.get("id") == TARGET_CHAT_ID:
        return JSONResponse({"ok": True})

    # ===== somente canal-fonte =====
    if chat.get("id") != SOURCE_CHAT_ID:
        return JSONResponse({"ok": True})

    STATE["last_raw_text"] = text

    if FLOW_THROUGH and text:
        await tg_send(TARGET_CHAT_ID, colorize_line(text))
        asyncio.create_task(aux_log({"ts": now_local().isoformat(), "type":"mirror", "text": text}))

    low = (text or "").lower()

    # "gatilho" para abrir
    has_trigger = bool(("entrada confirmada" in low) or SIDE_ALIASES_RE.search(low)) if text else False

    # "confirmação de fluxo" (fecha): G0 de primeira OU Gale (G1/G2)
    has_flow_confirm = False
    if text:
        has_flow_confirm = bool(
            SOURCE_G0_GREEN_RE.search(low) or G0_GREEN_HINT_RE.search(low) or
            SOURCE_WENT_GALE_RE.search(low) or PROGRESS_GALE_RE.search(low)
        )

    want_open = (FORCE_TRIGGER_OPEN or FORCE_OPEN_ON_ANY_SOURCE_MSG or has_trigger)

    # 1) Abrir (serial): só se não houver aberto
    if want_open and not STATE.get("open_signal"):
        STATE["last_open_src_msg_id"] = msg_id
        STATE["last_open_ts_epoch"] = msg_date_epoch
        save_state()
        await process_signal(text or "")
        # segue para possível confirmação (se esta mesma mensagem tentar fechar, vamos bloquear)

    # 2) Fechamento: só com confirmação do fluxo + regras de delay e outra mensagem
    if has_flow_confirm:
        osig = STATE.get("open_signal")
        if ONLY_ACCEPT_RESULT_IF_OPEN and not osig:
            return JSONResponse({"ok": True})

        # não fecha na mesma mensagem que abriu
        if ENFORCE_RESULT_AFTER_NEW_SOURCE_MESSAGE:
            last_open_id = STATE.get("last_open_src_msg_id")
            if (last_open_id is not None) and (msg_id == last_open_id):
                return JSONResponse({"ok": True})

        # respeita delay mínimo
        try:
            opened_epoch = int(STATE.get("last_open_ts_epoch") or 0)
            if (isinstance(msg_date_epoch, int)) and ((msg_date_epoch - opened_epoch) < MIN_RESULT_DELAY_SEC):
                return JSONResponse({"ok": True})
        except Exception:
            pass

        if osig:
            fonte_side  = osig.get("fonte_side")
            chosen_side = osig.get("chosen_side")
            our_res = derive_our_result_from_source_flow(text, fonte_side, chosen_side)

            if our_res in ("G","R"):
                # anuncia
                await announce_outcome(our_res, chosen_side)

                # placar
                if SCOREBOARD_FROM_SOURCE:
                    if our_res == "R":
                        STATE["daily_losses"] += 1
                        STATE["totals"]["reds"] = STATE["totals"].get("reds", 0) + 1
                        STATE["streak_green"] = 0
                    else:
                        STATE["totals"]["greens"] = STATE["totals"].get("greens", 0) + 1
                        STATE["streak_green"] = STATE.get("streak_green", 0) + 1

                    g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]
                    total = g + r
                    wr_day = (g/total*100.0) if total else 0.0
                    resumo = f"✅ {g} ⛔️ {r} 🎯 Acertamos {wr_day:.2f}%\n🥇 ESTAMOS A {STATE['streak_green']} GREENS SEGUIDOS ⏳"
                    digest = hashlib.md5(resumo.encode("utf-8")).hexdigest()
                    if digest != STATE.get("last_summary_hash"):
                        STATE["last_summary_hash"] = digest
                        await tg_send(TARGET_CHAT_ID, resumo)

                if LEARN_FROM_SOURCE_RESULTS and chosen_side:
                    rolling_append(chosen_side, our_res)
                    STATE["recent_results"].append(our_res)

                STATE["open_signal"] = None
                save_state()
            else:
                # ambíguo -> prorroga TTL e espera
                if EXTEND_TTL_ON_ACTIVITY and osig:
                    try:
                        new_exp = now_local() + timedelta(seconds=RESULT_GRACE_EXTEND_SEC)
                        cur_exp = osig.get("expires_at")
                        if cur_exp:
                            cur_dt = datetime.fromisoformat(cur_exp)
                            if new_exp < cur_dt:
                                new_exp = cur_dt
                        osig["expires_at"] = new_exp.isoformat()
                        save_state()
                    except Exception:
                        pass

        return JSONResponse({"ok": True})

    # 3) Sem abertura/fechamento: se há aberto e veio atividade sem confirmação -> prorroga TTL
    if EXTEND_TTL_ON_ACTIVITY and STATE.get("open_signal"):
        try:
            osig = STATE["open_signal"]
            if osig:
                new_exp = now_local() + timedelta(seconds=RESULT_GRACE_EXTEND_SEC)
                cur_exp = osig.get("expires_at")
                if cur_exp:
                    cur_dt = datetime.fromisoformat(cur_exp)
                    if new_exp < cur_dt:
                        new_exp = cur_dt
                osig["expires_at"] = new_exp.isoformat()
                save_state()
        except Exception:
            pass

    asyncio.create_task(aux_log({"ts": now_local().isoformat(), "type":"idle"}))
    return JSONResponse({"ok": True})