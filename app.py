# app.py — G0 autônomo no gatilho (independente do canal) + fechamento robusto (G1/G2=LOSS) + anti-vício
import os, json, asyncio, re, pytz, hashlib, random
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx, logging
from collections import deque

# ================== ENV ==================
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])   # canal-fonte (apenas gatilho)
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])   # canal destino (publicação)

TZ_NAME        = os.getenv("TZ", "UTC")
LOCAL_TZ       = pytz.timezone(TZ_NAME)

# Rolling / filtros (para métricas; não travam abertura quando FORCE_TRIGGER_OPEN=1)
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
MIN_GAP_SECS = int(os.getenv("MIN_GAP_SECS", "5"))    # anti-spam leve
FLOW_THROUGH = os.getenv("FLOW_THROUGH", "0") == "1"  # espelho visual opcional (não impacta decisão)
LOG_RAW      = os.getenv("LOG_RAW", "1") == "1"
DISABLE_WINDOWS = os.getenv("DISABLE_WINDOWS", "0") == "1"
DISABLE_RISK    = os.getenv("DISABLE_RISK", "1") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ====== Autônomo (decisão independente) ======
FORCE_TRIGGER_OPEN    = os.getenv("FORCE_TRIGGER_OPEN", "1") == "1"  # 1 = sempre abre no gatilho
AUTO_MIN_SIDE_SAMPLES = int(os.getenv("AUTO_MIN_SIDE_SAMPLES", "0"))
AUTO_MIN_SCORE        = float(os.getenv("AUTO_MIN_SCORE", "0.50"))
AUTO_MIN_MARGIN       = float(os.getenv("AUTO_MIN_MARGIN", "0.05"))

# Pesos do score
W_WR, W_HOUR, W_TREND = 0.70, 0.20, 0.10

# Exploração / anti-vício
EXPLORE_MARGIN = float(os.getenv("EXPLORE_MARGIN", "0.15"))  # delta pequeno
EXPLORE_EPS    = float(os.getenv("EXPLORE_EPS", "0.35"))     # 35% chance underdog
SIDE_STREAK_CAP= int(os.getenv("SIDE_STREAK_CAP", "3"))      # força alternar após streak
EPS_TIE        = float(os.getenv("EPS_TIE", "0.02"))         # empate técnico alterna

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

# Resultado: tokens e parser “último evento”
RESULT_GREEN_TOKENS_RE = re.compile(r"(✅|green\b|win\b)", re.I)
RESULT_RED_TOKENS_RE   = re.compile(r"(❌|red\b|lose\b|loss\b|derrota\b|perd)", re.I)

# Detectores de G1/G2 (se foi pro gale, G0 = LOSS)
G1_HINT_RE = re.compile(r"\b(g-?1|gale\s*1|primeiro\s*gale|indo\s+pro\s*g1|vamos\s+pro\s*g1|\bG1\b)\b", re.I)
G2_HINT_RE = re.compile(r"\b(g-?2|gale\s*2|segundo\s*gale|indo\s+pro\s*g2|vamos\s+pro\s*g2|\bG2\b)\b", re.I)

def parse_last_outcome(text: str) -> str | None:
    """
    Retorna 'G' (green) ou 'R' (loss) considerando G0 apenas.
    Regras:
      1) Se houver menção a G1/G2 -> G0 foi LOSS ('R'), mesmo que exista '✅' no texto.
      2) Caso contrário, vale o ÚLTIMO token de resultado no texto (✅ vs ❌).
    """
    if not text:
        return None
    low = text.lower()

    # Regra #1: foi para G1 ou G2? então G0 = LOSS
    if G1_HINT_RE.search(low) or G2_HINT_RE.search(low):
        return "R"

    # Regra #2: decide pelo último token
    marks = []
    for m in RESULT_GREEN_TOKENS_RE.finditer(text): marks.append((m.start(), "G"))
    for m in RESULT_RED_TOKENS_RE.finditer(text):   marks.append((m.start(), "R"))
    if not marks:
        return None
    marks.sort(key=lambda x: x[0])
    return marks[-1][1]

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

# ================== STATE ==================
STATE = {
    "messages": [], "last_reset_date": None,
    "pattern_roll": {}, "recent_results": deque(maxlen=TREND_LOOKBACK),
    "hour_stats": {}, "pattern_ban": {},
    "daily_losses": 0, "hourly_entries": {}, "cooldown_until": None,
    "recent_g0": [],
    "open_signal": None,  # {"ts","chosen_side","expires_at","text"}
    "totals": {"greens": 0, "reds": 0}, "streak_green": 0,
    "last_publish_ts": None, "processed_updates": deque(maxlen=500),
    "last_summary_hash": None,
    "side_history": deque(maxlen=20),  # sequência recente de lados
    "last_tiebreak": "banker",         # para alternância em empates
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
        STATE=data
    except Exception:
        save_state()
load_state()

# ================== MÉTRICAS / REGRAS ==================
def rolling_append(side:str,res:str):
    dq=STATE["pattern_roll"].setdefault(side,deque(maxlen=ROLLING_MAX)); dq.append(res)
    h=hour_str(); hst=STATE["hour_stats"].setdefault(h,{"g":0,"t":0}); hst["t"]+=1; (res=="G") and (hst.__setitem__("g", hst["g"]+1))

def side_wr(side: str, alpha: int = 8, beta: int = 8) -> tuple[float, int]:
    """
    Winrate suavizado (Bayes) com prior Beta(alpha,beta) ~ 0.5.
    Evita viciar no primeiro lado que ganhar.
    Retorna (wr_suavizado, n_real).
    """
    dq = STATE["pattern_roll"].get(side, deque())
    n = len(dq)
    g = sum(1 for x in dq if x == "G")
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
    if x < 0.0:  return 0.0
    if x > 1.0:  return 1.0
    return x

def compute_scores():
    wr_p,n_p=side_wr("player"); wr_b,n_b=side_wr("banker")
    hb, tb = hour_bonus(), trend_bonus()
    s_p = clamp01((W_WR*wr_p) + (W_HOUR*hb) + (W_TREND*tb))
    s_b = clamp01((W_WR*wr_b) + (W_HOUR*hb) + (W_TREND*tb))
    # vencedor padrão
    winner, s_win, s_lose = ("player",s_p,s_b) if s_p>=s_b else ("banker",s_b,s_p)
    dbg = {"score_player":s_p,"score_banker":s_b,"delta":abs(s_p-s_b), "n_p":n_p, "n_b":n_b}
    # empate técnico → alternar para não viciar
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
    await tg_send(TARGET_CHAT_ID, f"{big}\nNossa: {ns}")
    g, r, wr, streak = _score_snapshot()
    await tg_send(TARGET_CHAT_ID, f"📊 <b>Placar Geral</b>\n✅ {g}   ⛔️ {r}\n🎯 {wr:.2f}%  •  🔥 Streak {streak}")

async def publish_entry(final_side:str, dbg:str, window_tag:str):
    msg = (
        f"🚀 <b>ENTRADA AUTÔNOMA (G0)</b>{window_tag}\n"
        f"{'🔵 Player' if final_side=='player' else '🔴 Banker'}\n"
        f"{dbg}"
    )
    await tg_send(TARGET_CHAT_ID, msg)
    register_hourly_entry()

# ================== AUTÔNOMO NO GATILHO ==================
async def autonomous_open_from_trigger():
    allow_risk,_ = risk_allows()
    if not allow_risk and not FORCE_TRIGGER_OPEN:
        return

    # anti-spam
    last = STATE.get("last_publish_ts")
    if last:
        try:
            if (now_local() - datetime.fromisoformat(last)).total_seconds() < MIN_GAP_SECS:
                return
        except: pass

    # scores
    winner, s_win, s_lose, dbg = compute_scores()
    delta = dbg["delta"]

    # exploração leve (anti-vício)
    if delta < EXPLORE_MARGIN:
        underdog = "banker" if winner == "player" else "player"
        if random.random() < EXPLORE_EPS:
            winner, s_win = underdog, (dbg["score_banker"] if underdog == "banker" else dbg["score_player"])

    # quebra de streak muito longa
    hist = STATE.get("side_history")
    if isinstance(hist, deque) and len(hist) >= SIDE_STREAK_CAP and all(x == hist[-1] for x in list(hist)[-SIDE_STREAK_CAP:]):
        winner = "banker" if hist[-1] == "player" else "player"

    # checagens (se não for forçado)
    if not FORCE_TRIGGER_OPEN:
        if min(dbg["n_p"], dbg["n_b"]) < AUTO_MIN_SIDE_SAMPLES: return
        if s_win < AUTO_MIN_SCORE: return
        if delta < AUTO_MIN_MARGIN: return

    window_tag = "" if is_top_hour_now() else " <i>(fora da janela de ouro)</i>"
    header = f"Score P={dbg['score_player']:.2f} • Score B={dbg['score_banker']:.2f} • Δ={delta:.2f}"

    STATE["open_signal"] = {
        "ts": now_local().isoformat(),
        "chosen_side": winner,
        "expires_at": (now_local()+timedelta(minutes=2)).isoformat(),
        "text": header
    }
    STATE["last_publish_ts"] = now_local().isoformat()
    if isinstance(hist, deque):
        hist.append(winner)
    save_state()

    await publish_entry(winner, header, window_tag)

async def expire_open_if_needed():
    osig = STATE.get("open_signal")
    if not osig: return
    exp = osig.get("expires_at")
    if exp and datetime.fromisoformat(exp) <= now_local():
        STATE["open_signal"] = None
        save_state()
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"expired_no_result"}))

# ================== PROCESSAMENTO DE MENSAGENS ==================
async def aux_log(e:dict):
    STATE["messages"].append(e)
    if len(STATE["messages"])>5000: STATE["messages"]=STATE["messages"][-4000:]
    save_state()

async def process_signal(text: str):
    """
    Canal-fonte serve apenas de GATILHO. NÃO copiamos cor.
    Ao gatilho, decidimos de forma autônoma e abrimos G0.
    """
    low = (text or "").lower()
    if NOISE_RE.search(low):
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"noise","text":text}))
        return
    gatilho = ("entrada confirmada" in low) or SIDE_ALIASES_RE.search(low)
    if not gatilho:
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"no_trigger","text":text}))
        return
    await autonomous_open_from_trigger()

async def process_result(text: str):
    out = parse_last_outcome(text)
    if out not in ("G", "R"):  # não achou resultado
        return
    is_green = (out == "G")
    is_red   = (out == "R")

    osig = STATE.get("open_signal")
    if not osig:
        asyncio.create_task(aux_log({"ts":now_local().isoformat(),"type":"result_without_open","text":text}))
        return

    res = "G" if is_green else "R"
    chosen = osig.get("chosen_side")

    # registra rolling por lado escolhido
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

    # ban simples por sequência
    if chosen:
        tail = "".join(list(STATE["pattern_roll"].get(chosen, []))[-BAN_AFTER_CONSECUTIVE_R:])
        if tail and tail.endswith("R"*BAN_AFTER_CONSECUTIVE_R):
            until = now_local() + timedelta(hours=BAN_FOR_HOURS)
            STATE["pattern_ban"][chosen] = {"until": until.isoformat(), "reason": f"{BAN_AFTER_CONSECUTIVE_R} REDS seguidos"}

    # resumo (dedup) + anúncio grande
    g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]
    total = g + r
    wr_day = (g/total*100.0) if total else 0.0
    resumo = f"✅ {g} ⛔️ {r} 🎯 Acertamos {wr_day:.2f}%\n🥇 ESTAMOS A {STATE['streak_green']} GREENS SEGUIDOS ⏳"
    digest = hashlib.md5(resumo.encode("utf-8")).hexdigest()
    if digest != STATE.get("last_summary_hash"):
        STATE["last_summary_hash"] = digest
        await tg_send(TARGET_CHAT_ID, resumo)

    await announce_outcome(res, chosen)

    STATE["open_signal"] = None
    asyncio.create_task(aux_log({
        "ts": now_local().isoformat(),
        "type": "result_green" if is_green else "result_red",
        "text": text
    }))
    save_state()

# ================== RELATÓRIO ==================
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

async def housekeeping_loop():
    await asyncio.sleep(3)
    while True:
        try:
            await expire_open_if_needed()
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
    return {"ok": True, "service": "G0 autônomo no gatilho (independente do canal) [G1/G2=LOSS]"}

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

    # (opcional) espelho visual — NÃO interrompe o fluxo
    if FLOW_THROUGH:
        await tg_send(TARGET_CHAT_ID, colorize_line(text))
        asyncio.create_task(aux_log({"ts": now_local().isoformat(), "type":"mirror", "text": text}))

    # Resultados do canal → fechamento (usa parser com prioridade G1/G2=LOSS)
    if parse_last_outcome(text) in ("G","R"):
        await process_result(text)
        return JSONResponse({"ok": True})

    # Qualquer sinal válido vira GATILHO → abrir autônomo
    low = text.lower()
    if ("entrada confirmada" in low) or SIDE_ALIASES_RE.search(low):
        await process_signal(text)
        return JSONResponse({"ok": True})

    asyncio.create_task(aux_log({"ts": now_local().isoformat(), "type":"other", "text": text}))
    return JSONResponse({"ok": True})