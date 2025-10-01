# app.py — G0 AUTÔNOMO + fechamento rápido + override opcional + anti-502 + warm-up
import os, json, asyncio, re, pytz, hashlib, fastapi
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx, logging
from collections import deque

# ================== ENV ==================
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
TZ_NAME = os.getenv("TZ", "UTC")
LOCAL_TZ = pytz.timezone(TZ_NAME)

# Limiares (destravados; depois aperte)
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

FLOW_THROUGH     = os.getenv("FLOW_THROUGH", "1") == "1"
LOG_RAW          = os.getenv("LOG_RAW", "1") == "1"
DISABLE_WINDOWS  = os.getenv("DISABLE_WINDOWS", "0") == "1"
DISABLE_RISK     = os.getenv("DISABLE_RISK", "1") == "1"

STRICT_TRIGGER   = os.getenv("STRICT_TRIGGER", "0") == "1"
COUNT_STRATEGY_G0_ONLY = os.getenv("COUNT_STRATEGY_G0_ONLY", "1") == "1"
DEDUP_TTL_MIN    = int(os.getenv("DEDUP_TTL_MIN", "0"))
IGNORE_CHANNEL_OPENS = os.getenv("IGNORE_CHANNEL_OPENS", "1") == "1"   # << abre só autônomo

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Override (opcional)
DECISION_STRATEGY = os.getenv("DECISION_STRATEGY", "override").lower()
P_MIN_OVERRIDE = float(os.getenv("P_MIN_OVERRIDE", "0.62"))
MARGEM_MIN     = float(os.getenv("MARGEM_MIN", "0.08"))
W_WR, W_HOUR, W_TREND = float(os.getenv("W_WR", "0.70")), float(os.getenv("W_HOUR", "0.20")), float(os.getenv("W_TREND", "0.00"))
SOURCE_BIAS = float(os.getenv("SOURCE_BIAS", "0.15"))
MIN_SIDE_SAMPLES = int(os.getenv("MIN_SIDE_SAMPLES", "30"))

# AUTÔNOMO
AUTONOMOUS = os.getenv("AUTONOMOUS", "1") == "1"
AUTONOMOUS_INTERVAL_SEC = int(os.getenv("AUTONOMOUS_INTERVAL_SEC", "12"))
AUTO_MIN_SIDE_SAMPLES   = int(os.getenv("AUTO_MIN_SIDE_SAMPLES", "0"))
AUTO_MIN_SCORE          = float(os.getenv("AUTO_MIN_SCORE", "0.50"))
AUTO_MIN_MARGIN         = float(os.getenv("AUTO_MIN_MARGIN", "0.05"))
AUTO_RESULT_TIMEOUT_MIN = int(os.getenv("AUTO_RESULT_TIMEOUT_MIN", "2"))   # << FECHAMENTO RÁPIDO
AUTO_MAX_PARALLEL       = int(os.getenv("AUTO_MAX_PARALLEL", "1"))
AUTO_RESPECT_WINDOWS    = os.getenv("AUTO_RESPECT_WINDOWS", "0") == "1"

TRACE_MODE = os.getenv("TRACE_MODE", "1") == "1"

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

async def trace(msg: str):
    if TRACE_MODE:
        try: await tg_send(TARGET_CHAT_ID, f"🧪 <b>TRACE</b> {msg}")
        except Exception as e: log.error("trace error: %s", e)

# Anti-crash (evita 502)
@app.middleware("http")
async def safe_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        log.exception("Middleware caught: %s", e)
        await trace(f"exception: {type(e).__name__}: {e}")
        return JSONResponse({"ok": True}, status_code=200)

# ================== REGEX / PARSE ==================
SIDE_ALIASES_RE = re.compile(r"\b(player|banker|empate|p\b|b\b|azul|vermelho|blue|red)\b", re.I)
PATTERN_RE = re.compile(r"(banker|player|empate|azul|vermelho|blue|red|p\b|b\b|entrada\s*confirmada|\(?\s*g0\s*\)?)", re.I)
NOISE_RE   = re.compile(r"(bot\s*online|aposta\s*encerrada|analisando)", re.I)
GREEN_RE   = re.compile(r"(green|win|✅)", re.I)
RED_RE     = re.compile(r"(red|lose|perd|loss|derrota|❌)", re.I)

# G1/G2 (variações comuns)
G1_HINT_RE = re.compile(r"\b(g-?1|gale\s*1|primeiro\s*gale|indo\s+pro\s*g1|vamos\s+pro\s*g1|\bG1\b)\b", re.I)
G2_HINT_RE = re.compile(r"\b(g-?2|gale\s*2|segundo\s*gale|indo\s+pro\s*g2|vamos\s+pro\s*g2|\bG2\b)\b", re.I)

EMOJI_PLAYER_RE = re.compile(r"(🔵|🟦)", re.U)
EMOJI_BANKER_RE = re.compile(r"(🔴|🟥)", re.U)

def now_local(): return datetime.now(LOCAL_TZ)
def today_str(): return now_local().strftime("%Y-%m-%d")
def hour_str(dt=None): return (dt or now_local()).strftime("%H")
def hour_key(dt=None): return (dt or now_local()).strftime("%Y-%m-%d %H")

def colorize_line(text: str) -> str:
    t = re.sub(r"\b(player|p|azul|blue)\b", "🔵 Player", text, flags=re.I)
    t = re.sub(r"\b(banker|b|vermelho|red)\b", "🔴 Banker", t, flags=re.I)
    t = re.sub(r"\bempate\b", "🟡 Empate", t, flags=re.I)
    t = re.sub(r"\( ?g0 ?\)", "(G0)", t, flags=re.I)
    return t

def normalize_side(s: str|None) -> str|None:
    if not s: return None
    s = s.lower()
    if s in ("player","p","azul","blue"): return "player"
    if s in ("banker","b","vermelho","red"): return "banker"
    if s == "empate": return "empate"
    return None

def extract_side(text: str) -> str|None:
    m = SIDE_ALIASES_RE.search(text or "")
    if m:
        side = normalize_side(m.group(0))
        if side: return side
    if EMOJI_PLAYER_RE.search(text or ""): return "player"
    if EMOJI_BANKER_RE.search(text or ""): return "banker"
    return None

# ================== STATE ==================
STATE = {
    "messages": [], "last_reset_date": None,
    "pattern_roll": {}, "recent_results": deque(maxlen=TREND_LOOKBACK),
    "hour_stats": {}, "pattern_ban": {},
    "daily_losses": 0, "hourly_entries": {}, "cooldown_until": None,
    "recent_g0": [],
    "open_signal": None,  # {"ts","text","pattern","fonte_side","chosen_side","autonomous":bool,"expires_at":iso}
    "totals": {"greens": 0, "reds": 0}, "streak_green": 0,
    "last_publish_ts": None, "processed_updates": deque(maxlen=500),
    "last_summary_hash": None, "recent_signal_hashes": {},
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
        for k in ("hourly_entries","pattern_ban","messages","totals","recent_signal_hashes"):
            data.setdefault(k, {} if k!="messages" else [])
        STATE=data
    except Exception: save_state()
load_state()

# ================== MÉTRICAS / REGRAS ==================
async def aux_log(e:dict):
    STATE["messages"].append(e)
    if len(STATE["messages"])>5000: STATE["messages"]=STATE["messages"][-4000:]
    save_state()

def rolling_append(pat:str,res:str):
    dq=STATE["pattern_roll"].setdefault(pat,deque(maxlen=ROLLING_MAX)); dq.append(res)
    h=hour_str(); hst=STATE["hour_stats"].setdefault(h,{"g":0,"t":0}); hst["t"]+=1; (res=="G") and (hst.__setitem__("g", hst["g"]+1))

def rolling_wr(pat:str)->float:
    dq=STATE["pattern_roll"].get(pat);  return 0.0 if not dq else sum(1 for x in dq if x=="G")/len(dq)

def pattern_recent_tail(p,k): dq=STATE["pattern_roll"].get(p) or []; return "".join(list(dq)[-k:])

def ban_pattern(p,why):
    until=now_local()+timedelta(hours=BAN_FOR_HOURS)
    STATE["pattern_ban"][p]={"until":until.isoformat(),"reason":why}; save_state()

def is_banned(p)->bool:
    b=STATE["pattern_ban"].get(p); return bool(b) and datetime.fromisoformat(b["until"])>now_local()

def cleanup_expired_bans():
    for p,b in list(STATE["pattern_ban"].items()):
        if datetime.fromisoformat(b["until"])<=now_local(): STATE["pattern_ban"].pop(p,None)

def daily_reset_if_needed():
    if STATE.get("last_reset_date")!=today_str():
        STATE["last_reset_date"]=today_str()
        STATE["daily_losses"]=0; STATE["hourly_entries"]={}; STATE["cooldown_until"]=None
        STATE["totals"]={"greens":0,"reds":0}; STATE["streak_green"]=0; STATE["open_signal"]=None
        cleanup_expired_bans(); save_state()

def cooldown_active(): cu=STATE.get("cooldown_until"); return bool(cu) and datetime.fromisoformat(cu)>now_local()
def start_cooldown(): STATE["cooldown_until"]=(now_local()+timedelta(minutes=COOLDOWN_MINUTES)).isoformat(); save_state()
def streak_guard_triggered(): k=STREAK_GUARD_LOSSES; r=STATE.get("recent_g0",[]); return k>0 and len(r)>=k and all(x=="R" for x in r[-k:])
def hourly_cap_ok(): return STATE["hourly_entries"].get(hour_key(),0) < HOURLY_CAP
def register_hourly_entry(): k=hour_key(); STATE["hourly_entries"][k]=STATE["hourly_entries"].get(k,0)+1; save_state()

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
    if side not in ("player","banker"): return False,0.0,0
    dq=STATE["pattern_roll"].get(side,deque()); n=len(dq)
    if n<MIN_SAMPLES_BEFORE_FILTER: return True,0.0,n   # warm-up
    wr=sum(1 for x in dq if x=="G")/n if n else 0.0
    return (wr>=MIN_G0),wr,n

def trend_allows()->tuple[bool,float,int]:
    dq=list(STATE["recent_results"]); n=len(dq)
    if n<max(10,int(TREND_LOOKBACK/2)): return True,1.0,n
    wr=sum(1 for x in dq if x=="G")/n;  return (wr>=TREND_MIN_WR),wr,n

def window_allows()->tuple[bool,float]:
    ok=is_top_hour_now(); return ok, 1.0 if ok else 0.0

def streak_ban_allows(p)->tuple[bool,str]:
    if is_banned(p): return False,"pattern_banned"
    tail=pattern_recent_tail(p,BAN_AFTER_CONSECUTIVE_R)
    if tail and tail.endswith("R"*BAN_AFTER_CONSECUTIVE_R):
        ban_pattern(p,f"{BAN_AFTER_CONSECUTIVE_R} REDS seguidos"); return False,"tail_rr"
    return True,""

def risk_allows()->tuple[bool,str]:
    if DISABLE_RISK: return True,""
    if STATE["daily_losses"]>=DAILY_STOP_LOSS: return False,"stop_daily"
    if cooldown_active(): return False,"cooldown"
    if streak_guard_triggered(): start_cooldown(); return False,"streak_guard"
    return True,""

# Scores
def side_wr(side:str)->tuple[float,int]:
    dq=STATE["pattern_roll"].get(side,deque()); n=len(dq)
    return (0.0,0) if n==0 else (sum(1 for x in dq if x=="G")/n, n)

def hour_bonus(): return 1.0 if is_top_hour_now() else 0.0
def trend_bonus():
    dq=list(STATE["recent_results"]); n=len(dq)
    if n==0: return 0.0
    wr=sum(1 for x in dq if x=="G")/n; return max(0.0,min(1.0,wr))

def compute_scores(fonte_side:str|None):
    wr_p,n_p=side_wr("player"); wr_b,n_b=side_wr("banker")
    hb, tb = hour_bonus(), trend_bonus()
    bp = SOURCE_BIAS if fonte_side=="player" else 0.0
    bb = SOURCE_BIAS if fonte_side=="banker" else 0.0
    s_p=max(0.0,min(1.0,(W_WR*wr_p)+(W_HOUR*hb)+(W_TREND*tb)+bp))
    s_b=max(0.0,min(1.0,(W_WR*wr_b)+(W_HOUR*hb)+(W_TREND*tb)+bb))
    winner, s_win, s_lose = ("player",s_p,s_b) if s_p>=s_b else ("banker",s_b,s_p)
    delta=s_win-s_lose
    dbg={"wr_player":wr_p,"n_player":n_p,"wr_banker":wr_b,"n_banker":n_b,"hour_bonus":hb,"trend_bonus":tb,"score_player":s_p,"score_banker":s_b,"delta":delta}
    return winner, s_win, delta, dbg

def compute_override(text:str):
    fonte=extract_side(text)
    if fonte in (None,"empate"): return None, {"reason":"no_override"}
    winner, s_win, delta, dbg = compute_scores(fonte)
    passes=(s_win>=P_MIN_OVERRIDE) and (delta>=MARGEM_MIN) and (min(dbg["n_player"],dbg["n_banker"])>=MIN_SIDE_SAMPLES)
    dbg.update({"fonte":fonte,"passes":passes,"threshold":P_MIN_OVERRIDE,"margin":MARGEM_MIN})
    return (winner if passes else None), dbg

# ================== PUBLICAÇÃO ==================
async def publish_entry(final_side:str, fonte_side:str|None, header:str, extra_line:str=""):
    pretty = f"{'🔵 Player' if final_side=='player' else '🔴 Banker'}\n{header}"
    ok_win,_ = window_allows()
    allow_g0, wr_g0, n_g0 = g0_side_allows(final_side)
    window_tag = "" if ok_win else " <i>(fora da janela de ouro)</i>"
    msg = (
        f"🚀 <b>{'ENTRADA AUTÔNOMA' if fonte_side is None else 'ENTRADA ABERTA (G0)'}</b>\n"
        f"✅ G0 {wr_g0*100:.1f}% ({n_g0} am.){window_tag}{extra_line}\n{pretty}"
    )
    await tg_send(TARGET_CHAT_ID, msg)
    register_hourly_entry()

# ================== SINAL DO CANAL (APENAS APRENDER) ==================
def dedup_recent_signal(text:str)->bool:
    if DEDUP_TTL_MIN<=0: return False
    t=re.sub(r"\s+"," ",(text or "").lower()).strip()
    h=hashlib.sha1(t.encode("utf-8")).hexdigest()
    now=now_local()
    for k,v in list(STATE["recent_signal_hashes"].items()):
        if (now-datetime.fromisoformat(v)).total_seconds()>DEDUP_TTL_MIN*60:
            STATE["recent_signal_hashes"].pop(k,None)
    if h in STATE["recent_signal_hashes"]: return True
    STATE["recent_signal_hashes"][h]=now.isoformat(); save_state(); return False

async def process_signal(text:str):
    daily_reset_if_needed()
    low=(text or "").lower()
    if NOISE_RE.search(low): 
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"noise","text":text})); 
        return
    if STRICT_TRIGGER:
        gat = ("entrada confirmada" in low) or re.search(r"\b\(?\s*g0\s*\)?\b", low)
    else:
        gat = ("entrada confirmada" in low) or re.search(r"\b\(?\s*g0\s*\)?\b", low) or (extract_side(text) in ("player","banker"))
    if not gat: 
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"no_trigger","text":text})); 
        return
    if dedup_recent_signal(text): 
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"dupe_signal","text":text})); 
        return
    if IGNORE_CHANNEL_OPENS:
        # não abre por canal; só aprende resultado depois
        return

    # (se quiser permitir híbrido, basta remover este return e manter o resto)
    return

# ================== AUTÔNOMO ==================
async def autonomous_try_open():
    if STATE.get("open_signal"):  # silencioso; sem TRACE
        return
    if AUTO_MAX_PARALLEL<=0: return

    allow_risk, why = risk_allows()
    if not allow_risk: return
    if not hourly_cap_ok(): return
    if AUTO_RESPECT_WINDOWS and not is_top_hour_now(): return

    wr_p,n_p=side_wr("player"); wr_b,n_b=side_wr("banker")
    if min(n_p,n_b)<AUTO_MIN_SIDE_SAMPLES: return

    winner, s_win, delta, dbg = compute_scores(None)
    if (s_win<AUTO_MIN_SCORE) or (delta<AUTO_MIN_MARGIN): return

    allow_g0, wr_g0, n_g0 = g0_side_allows(winner)
    if not allow_g0: return

    last=STATE.get("last_publish_ts")
    if last:
        try:
            if (now_local()-datetime.fromisoformat(last)).total_seconds()<MIN_GAP_SECS: return
        except: pass

    header=f"(G0) entrada técnica — escP={dbg['score_player']:.2f} escB={dbg['score_banker']:.2f} (Δ={dbg['delta']:.2f})"
    STATE["open_signal"]={
        "ts":now_local().isoformat(),
        "text":header,
        "pattern":winner,
        "fonte_side":None,
        "chosen_side":winner,
        "autonomous":True,
        "expires_at":(now_local()+timedelta(minutes=AUTO_RESULT_TIMEOUT_MIN)).isoformat()
    }
    STATE["last_publish_ts"]=now_local().isoformat(); save_state()
    await publish_entry(winner, None, header, "")

# Fechamento RÁPIDO ao detectar G1/G2 (ou Green/Red)
async def settle_open_g0_due_to_gale(text:str, reason:str="canal_gale"):
    open_sig=STATE.get("open_signal")
    if not open_sig: return
    chosen=open_sig.get("chosen_side") or open_sig.get("pattern")
    fonte =open_sig.get("fonte_side")  # None no autônomo
    if fonte not in ("player","banker"):
        # autônomo sem lado da fonte: não há como inferir assertividade pelo gale; ignora (neutro)
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"auto_gale_hint_ignored","text":text}))
        return
    res = "R" if (chosen==fonte) else "G"
    rolling_append(chosen,res); STATE["recent_results"].append(res)
    STATE["recent_g0"]=(STATE.get("recent_g0",[])+[res])[-10:]
    if res=="R": STATE["daily_losses"]=STATE.get("daily_losses",0)+1
    if COUNT_STRATEGY_G0_ONLY:
        if res=="G": STATE["totals"]["greens"]+=1; STATE["streak_green"]=STATE.get("streak_green",0)+1
        else: STATE["totals"]["reds"]+=1; STATE["streak_green"]=0
    STATE["open_signal"]=None; save_state()
    asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"g0_settled_by_gale","result":res,"text":text}))

async def process_result(text:str):
    is_green=bool(GREEN_RE.search(text)); is_red=bool(RED_RE.search(text))
    if not (is_green or is_red): return
    open_sig=STATE.get("open_signal")
    if COUNT_STRATEGY_G0_ONLY and open_sig is None:
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"late_result_ignored","text":text})); return
    res="G" if is_green else "R"
    chosen=(open_sig.get("chosen_side") or open_sig.get("pattern")) if open_sig else None
    if chosen: rolling_append(chosen,res)
    STATE["recent_results"].append(res)
    STATE["recent_g0"]=(STATE.get("recent_g0",[])+[res])[-10:]
    if res=="R": STATE["daily_losses"]=STATE.get("daily_losses",0)+1
    if res=="G": STATE["totals"]["greens"]+=1; STATE["streak_green"]=STATE.get("streak_green",0)+1
    else: STATE["totals"]["reds"]+=1; STATE["streak_green"]=0
    STATE["open_signal"]=None; save_state()

async def expire_open_if_needed():
    open_sig=STATE.get("open_signal")
    if not open_sig: return
    exp=open_sig.get("expires_at")
    if exp and datetime.fromisoformat(exp)<=now_local():
        # timeout curto → fecha sem contar
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"auto_expired_no_result","chosen":open_sig.get("chosen_side")}))
        STATE["open_signal"]=None; save_state()

# ================== RELATÓRIO / LOOPS ==================
def build_report():
    g,r=STATE["totals"]["greens"],STATE["totals"]["reds"]; total=g+r; wr_day=(g/total*100.0) if total else 0.0
    rows=[]
    for p,dq in STATE.get("pattern_roll",{}).items():
        n=len(dq)
        if n>=10:
            rows.append((p, sum(1 for x in dq if x=="G")/n, n))
    rows.sort(key=lambda x:x[1], reverse=True)
    tops=rows[:5]
    cleanup_expired_bans()
    bans=[f"• {p} (até {datetime.fromisoformat(b['until']).strftime('%d/%m %H:%M')})" for p,b in STATE.get("pattern_ban",{}).items()]
    lines=[f"<b>📊 Relatório Diário ({today_str()})</b>", f"Dia: <b>{g}G / {r}R</b> (WR: {wr_day:.1f}%)", "", "<b>Top lados:</b>"]
    lines += [f"• {p} → {wr*100:.1f}% ({n})" for p,wr,n in tops] or ["• Sem dados."]
    lines += ["", "<b>Lados banidos:</b>"] + (bans or ["• Nenhum."])
    return "\n".join(lines)

async def daily_report_loop():
    await asyncio.sleep(5)
    while True:
        try:
            n=now_local()
            if n.hour==0 and n.minute==0:
                daily_reset_if_needed()
                await tg_send(TARGET_CHAT_ID, build_report()); await asyncio.sleep(65)
            else:
                await asyncio.sleep(10)
        except: await asyncio.sleep(5)

async def autonomous_loop():
    await asyncio.sleep(3)
    while True:
        try:
            if AUTONOMOUS:
                await expire_open_if_needed()
                await autonomous_try_open()
            await asyncio.sleep(AUTONOMOUS_INTERVAL_SEC)
        except: await asyncio.sleep(2)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(daily_report_loop())
    asyncio.create_task(autonomous_loop())

# ================== ROTAS ==================
@app.get("/")
async def root():
    return {"ok": True, "service": "G0 autônomo + fast close + anti-502"}

@app.get("/healthz")
async def healthz(): return {"ok": True}

@app.post("/webhook")
@app.post("/webhook/{secret}")
async def webhook(req: Request, secret: str|None=None):
    # tolerante a payload estranho
    try:
        update = await req.json()
    except Exception:
        body=(await req.body())[:1000]
        if LOG_RAW: log.info("RAW (non-json): %r", body)
        await trace("payload não-JSON no /webhook (ignorado)")
        return JSONResponse({"ok": True})

    if LOG_RAW: log.info("RAW UPDATE: %s", update)

    up_id=update.get("update_id")
    if up_id is not None:
        if up_id in STATE["processed_updates"]: return JSONResponse({"ok": True})
        STATE["processed_updates"].append(up_id)

    msg = update.get("channel_post") or update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not text or chat_id != SOURCE_CHAT_ID:
        return JSONResponse({"ok": True})

    if FLOW_THROUGH:
        await tg_send(TARGET_CHAT_ID, colorize_line(text))
        asyncio.create_task(aux_log({"ts": now_local().isoformat(), "type":"mirror", "text": text}))

    # FECHAMENTO RÁPIDO
    if G1_HINT_RE.search(text) or G2_HINT_RE.search(text):
        await settle_open_g0_due_to_gale(text, reason="canal_gale")
        return JSONResponse({"ok": True})
    if GREEN_RE.search(text) or RED_RE.search(text):
        await process_result(text)
        return JSONResponse({"ok": True})

    # (opcional) processamento do “sinal” do canal — aqui ignorado se IGNORE_CHANNEL_OPENS=1
    await process_signal(text)
    return JSONResponse({"ok": True})