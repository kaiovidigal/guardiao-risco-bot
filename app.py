# app.py — G0 AUTÔNOMO + gatilho do canal + GREEN/LOSS gigante + placar + fast-close
import os, json, asyncio, re, pytz, hashlib
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx, logging
from collections import deque

# ================== ENV ==================
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
TZ_NAME        = os.getenv("TZ", "UTC")
LOCAL_TZ       = pytz.timezone(TZ_NAME)

# destravado (ajuste depois p/ sniper)
MIN_G0 = float(os.getenv("MIN_G0", "0.92"))
MIN_SAMPLES_BEFORE_FILTER = int(os.getenv("MIN_SAMPLES_BEFORE_FILTER", "20"))
ROLLING_MAX = int(os.getenv("ROLLING_MAX", "800"))
TREND_LOOKBACK = int(os.getenv("TREND_LOOKBACK", "40"))
TREND_MIN_WR   = float(os.getenv("TREND_MIN_WR", "0.80"))

TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.85"))
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "20"))
TOP_HOURS_COUNT       = int(os.getenv("TOP_HOURS_COUNT", "8"))

BAN_AFTER_CONSECUTIVE_R = int(os.getenv("BAN_AFTER_CONSECUTIVE_R", "1"))
BAN_FOR_HOURS           = int(os.getenv("BAN_FOR_HOURS", "4"))

DAILY_STOP_LOSS   = int(os.getenv("DAILY_STOP_LOSS", "999"))
STREAK_GUARD_LOSSES = int(os.getenv("STREAK_GUARD_LOSSES", "0"))
COOLDOWN_MINUTES  = int(os.getenv("COOLDOWN_MINUTES", "15"))
HOURLY_CAP        = int(os.getenv("HOURLY_CAP", "20"))
MIN_GAP_SECS      = int(os.getenv("MIN_GAP_SECS", "5"))

FLOW_THROUGH     = os.getenv("FLOW_THROUGH", "0") == "1"  # eco do canal (debug)
LOG_RAW          = os.getenv("LOG_RAW", "1") == "1"
DISABLE_WINDOWS  = os.getenv("DISABLE_WINDOWS", "0") == "1"
DISABLE_RISK     = os.getenv("DISABLE_RISK", "1") == "1"

# Nunca abrir por canal: usamos como GATILHO, decisão é autônoma
IGNORE_CHANNEL_OPENS = True

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# AUTÔNOMO
AUTONOMOUS = os.getenv("AUTONOMOUS", "1") == "1"
AUTONOMOUS_INTERVAL_SEC = int(os.getenv("AUTONOMOUS_INTERVAL_SEC", "12"))
AUTO_MIN_SIDE_SAMPLES   = int(os.getenv("AUTO_MIN_SIDE_SAMPLES", "0"))
AUTO_MIN_SCORE          = float(os.getenv("AUTO_MIN_SCORE", "0.50"))
AUTO_MIN_MARGIN         = float(os.getenv("AUTO_MIN_MARGIN", "0.05"))
AUTO_RESULT_TIMEOUT_MIN = int(os.getenv("AUTO_RESULT_TIMEOUT_MIN", "2"))
AUTO_MAX_PARALLEL       = int(os.getenv("AUTO_MAX_PARALLEL", "1"))
AUTO_RESPECT_WINDOWS    = os.getenv("AUTO_RESPECT_WINDOWS", "0") == "1"

# Score weights (simples)
W_WR, W_HOUR, W_TREND = 0.70, 0.20, 0.10

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
GREEN_RE   = re.compile(r"(green|win|✅)", re.I)
RED_RE     = re.compile(r"(red|lose|perd|loss|derrota|❌)", re.I)
G1_HINT_RE = re.compile(r"\b(g-?1|gale\s*1|primeiro\s*gale|indo\s+pro\s*g1|vamos\s+pro\s*g1|\bG1\b)\b", re.I)
G2_HINT_RE = re.compile(r"\b(g-?2|gale\s*2|segundo\s*gale|indo\s+pro\s*g2|vamos\s+pro\s*g2|\bG2\b)\b", re.I)
EMOJI_PLAYER_RE = re.compile(r"(🔵|🟦)", re.U)
EMOJI_BANKER_RE = re.compile(r"(🔴|🟥)", re.U)

def now_local(): return datetime.now(LOCAL_TZ)
def today_str(): return now_local().strftime("%Y-%m-%d")
def hour_str(dt=None): return (dt or now_local()).strftime("%H")
def hour_key(dt=None): return (dt or now_local()).strftime("%Y-%m-%d %H")

def colorize_line(text: str) -> str:
    t = text
    t = re.sub(r"\b(player|p|azul|blue)\b", "🔵 Player", t, flags=re.I)
    t = re.sub(r"\b(banker|b|vermelho|red)\b", "🔴 Banker", t, flags=re.I)
    t = re.sub(r"\bempate\b", "🟡 Empate", t, flags=re.I)
    return t

def normalize_side(s: str|None) -> str|None:
    if not s: return None
    s = s.lower()
    if s in ("player","p","azul","blue"): return "player"
    if s in ("banker","b","vermelho","red"): return "banker"
    if s == "empate": return "empate"
    return None

# ================== STATE ==================
STATE = {
    "messages": [], "last_reset_date": None,
    "pattern_roll": {}, "recent_results": deque(maxlen=TREND_LOOKBACK),
    "hour_stats": {}, "pattern_ban": {},
    "daily_losses": 0, "hourly_entries": {}, "cooldown_until": None,
    "recent_g0": [],
    "open_signal": None,  # {"ts","text","chosen_side","autonomous":bool,"expires_at":iso}
    "totals": {"greens": 0, "reds": 0}, "streak_green": 0,
    "last_publish_ts": None, "processed_updates": deque(maxlen=500),
}

def _ensure_dir(path): d=os.path.dirname(path);  (d and not os.path.exists(d)) and os.makedirs(d, exist_ok=True)

def save_state():
    def _c(o):
        from collections import deque as _dq
        if isinstance(o,_dq): return list(o)
        if isinstance(o,dict): return {k:_c(v) for k,v in o.items()}
        if isinstance(o,list): return [_c(x) for x in o]
        return o
    _ensure_dir(STATE_PATH); tmp=STATE_PATH+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(_c(STATE), f, ensure_ascii=False)
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
        for k in ("hourly_entries","pattern_ban","messages","totals"):
            data.setdefault(k, {} if k!="messages" else [])
        STATE=data
    except Exception: save_state()
load_state()

# ================== MÉTRICAS / REGRAS ==================
async def aux_log(e:dict):
    STATE["messages"].append(e)
    if len(STATE["messages"])>5000: STATE["messages"]=STATE["messages"][-4000:]
    save_state()

def rolling_append(side:str,res:str):
    dq=STATE["pattern_roll"].setdefault(side,deque(maxlen=ROLLING_MAX)); dq.append(res)
    h=hour_str(); hst=STATE["hour_stats"].setdefault(h,{"g":0,"t":0}); hst["t"]+=1; (res=="G") and (hst.__setitem__("g", hst["g"]+1))

def side_wr(side:str)->tuple[float,int]:
    dq=STATE["pattern_roll"].get(side,deque()); n=len(dq)
    return (0.0,0) if n==0 else (sum(1 for x in dq if x=="G")/n, n)

def is_top_hour_now()->bool:
    if DISABLE_WINDOWS: return True
    stats=STATE.get("hour_stats",{}); items=[(h,s) for h,s in stats.items() if s["t"]>=TOP_HOURS_MIN_SAMPLES]
    if not items: return True
    ranked=sorted(items,key=lambda kv:(kv[1]["g"]/kv[1]["t"]),reverse=True)
    top=[]; 
    for h,s in ranked:
        wr=s["g"]/s["t"]
        if wr>=TOP_HOURS_MIN_WINRATE: top.append(h)
        if len(top)>=TOP_HOURS_COUNT: break
    return hour_str() in top if top else True

def g0_side_allows(side:str)->tuple[bool,float,int]:
    dq=STATE["pattern_roll"].get(side,deque()); n=len(dq)
    if n<MIN_SAMPLES_BEFORE_FILTER: return True,0.0,n
    wr=sum(1 for x in dq if x=="G")/n if n else 0.0
    return (wr>=MIN_G0),wr,n

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

def compute_scores():
    wr_p,n_p=side_wr("player"); wr_b,n_b=side_wr("banker")
    hb, tb = hour_bonus(), trend_bonus()
    s_p=max(0.0,min(1.0,(W_WR*wr_p)+(W_HOUR*hb)+(W_TREND*tb)))
    s_b=max(0.0,min(1.0,(W_WR*wr_b)+(W_HOUR*hb)+(W_TREND*tb)))
    winner, s_win, s_lose = ("player",s_p,s_b) if s_p>=s_b else ("banker",s_b,s_p)
    return winner, s_win, s_lose, {"score_player":s_p,"score_banker":s_b,"delta":abs(s_p-s_b)}

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
    ns = {"player":"🔵 Player","banker":"🔴 Banker"}.get((chosen_side or "").lower(),"?")
    await tg_send(TARGET_CHAT_ID, f"{big}\nNossa: {ns}")
    g, r, wr, streak = _score_snapshot()
    await tg_send(TARGET_CHAT_ID, f"📊 <b>Placar Geral</b>\n✅ {g}   ⛔️ {r}\n🎯 {wr:.2f}%  •  🔥 Streak {streak}")

async def publish_entry(final_side:str, header:str):
    ok_win = is_top_hour_now()
    allow_g0, wr_g0, n_g0 = g0_side_allows(final_side)
    window_tag = "" if ok_win else " <i>(fora da janela de ouro)</i>"
    msg = (
        f"🚀 <b>ENTRADA AUTÔNOMA (G0)</b>\n"
        f"✅ G0 {wr_g0*100:.1f}% ({n_g0} am.){window_tag}\n"
        f"{'🔵 Player' if final_side=='player' else '🔴 Banker'}\n{header}"
    )
    await tg_send(TARGET_CHAT_ID, msg)
    register_hourly_entry()

# ================== AUTÔNOMO ==================
async def autonomous_try_open():
    if STATE.get("open_signal"):  # já existe aberto
        return
    if AUTO_MAX_PARALLEL <= 0:
        return
    allow_risk,_ = risk_allows()
    if not allow_risk: return
    if AUTO_RESPECT_WINDOWS and not is_top_hour_now(): return

    wr_p,n_p = side_wr("player"); wr_b,n_b = side_wr("banker")
    if min(n_p,n_b) < AUTO_MIN_SIDE_SAMPLES: return

    winner, s_win, s_lose, dbg = compute_scores()
    delta = dbg["delta"]
    if (s_win < AUTO_MIN_SCORE) or (delta < AUTO_MIN_MARGIN): return

    allow_g0, _, _ = g0_side_allows(winner)
    if not allow_g0: return

    last = STATE.get("last_publish_ts")
    if last:
        try:
            if (now_local() - datetime.fromisoformat(last)).total_seconds() < MIN_GAP_SECS:
                return
        except: pass

    header = f"Score P={dbg['score_player']:.2f} • Score B={dbg['score_banker']:.2f} • Δ={delta:.2f}"
    STATE["open_signal"] = {
        "ts": now_local().isoformat(),
        "chosen_side": winner,
        "autonomous": True,
        "expires_at": (now_local()+timedelta(minutes=AUTO_RESULT_TIMEOUT_MIN)).isoformat()
    }
    STATE["last_publish_ts"] = now_local().isoformat()
    save_state()
    await publish_entry(winner, header)

async def expire_open_if_needed():
    osig = STATE.get("open_signal")
    if not osig: return
    exp = osig.get("expires_at")
    if exp and datetime.fromisoformat(exp) <= now_local():
        # timeout curto → fecha neutro (não conta)
        STATE["open_signal"] = None
        save_state()
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"expired_no_result"}))

# ================== PROCESSAMENTO DE MENSAGENS ==================
async def process_signal(text: str):
    # Canal vira GATILHO (não copiamos cor)
    low = (text or "").lower()
    if NOISE_RE.search(low): return
    gatilho = ("entrada confirmada" in low) or re.search(r"\b(banker|player|empate|bac\s*bo|dados)\b", low)
    if not gatilho: return
    await autonomous_try_open()  # abre G0 na hora, decisão própria

async def process_result(text: str):
    is_green = bool(GREEN_RE.search(text))
    is_red   = bool(RED_RE.search(text))
    if not (is_green or is_red): return

    osig = STATE.get("open_signal")
    if not osig:
        # sem G0 aberto → ignora
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"result_without_open","text":text}))
        return

    res = "G" if is_green else "R"
    chosen = osig.get("chosen_side")

    # registra
    if chosen: rolling_append(chosen, res)
    STATE["recent_results"].append(res)
    STATE["recent_g0"] = (STATE.get("recent_g0", []) + [res])[-10:]

    if res == "R":
        STATE["daily_losses"] += 1
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0
    else:
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] += 1

    await announce_outcome(res, chosen)
    STATE["open_signal"] = None
    save_state()

# ================== LOOPS ==================
def build_report():
    g,r=STATE["totals"]["greens"],STATE["totals"]["reds"]; t=g+r; wr=(g/t*100.0) if t else 0.0
    return f"<b>📊 Relatório Diário ({today_str()})</b>\nDia: <b>{g}G / {r}R</b> (WR: {wr:.1f}%)"

async def daily_report_loop():
    await asyncio.sleep(5)
    while True:
        try:
            n = now_local()
            if n.hour==0 and n.minute==0:
                # reset diário
                STATE["daily_losses"]=0; STATE["hourly_entries"]={}; STATE["streak_green"]=0
                await tg_send(TARGET_CHAT_ID, build_report())
                await asyncio.sleep(65)
            else:
                await asyncio.sleep(10)
        except Exception:
            await asyncio.sleep(5)

async def autonomous_loop():
    await asyncio.sleep(3)
    while True:
        try:
            if AUTONOMOUS:
                await expire_open_if_needed()
                await autonomous_try_open()
            await asyncio.sleep(AUTONOMOUS_INTERVAL_SEC)
        except Exception:
            await asyncio.sleep(2)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(daily_report_loop())
    asyncio.create_task(autonomous_loop())

# ================== ROTAS ==================
@app.get("/")
async def root():
    return {"ok": True, "service": "G0 autônomo + gatilho do canal + fast close"}

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
    if chat.get("id") != SOURCE_CHAT_ID:
        return JSONResponse({"ok": True})

    text = (msg.get("text") or "").strip()
    if not text: return JSONResponse({"ok": True})

    if FLOW_THROUGH:
        await tg_send(TARGET_CHAT_ID, colorize_line(text))

    # Resultados do canal (fechamento rápido)
    if GREEN_RE.search(text) or RED_RE.search(text):
        await process_result(text)
        return JSONResponse({"ok": True})

    # Gatilho (qualquer sinal válido)
    await process_signal(text)
    return JSONResponse({"ok": True})