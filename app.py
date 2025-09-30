# app.py — pipeline G0 com 5 especialistas + dedup e contagem real
import os, json, asyncio, re, pytz, hashlib
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
import httpx
import logging
from collections import deque

# ============= ENV =============
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])

TZ_NAME = os.getenv("TZ", "UTC")
LOCAL_TZ = pytz.timezone(TZ_NAME)

# Estratégia / limites (ajuste no Render)
MIN_G0 = float(os.getenv("MIN_G0", "0.90"))                 # corte do G0 (rolling)
MIN_SAMPLES_BEFORE_FILTER = int(os.getenv("MIN_SAMPLES_BEFORE_FILTER", "20"))
ROLLING_MAX = int(os.getenv("ROLLING_MAX", "600"))

# Tendência recente (especialista #2)
TREND_LOOKBACK = int(os.getenv("TREND_LOOKBACK", "40"))     # últimas N entradas aprovadas
TREND_MIN_WR   = float(os.getenv("TREND_MIN_WR", "0.80"))   # WR mínimo no lookback

# Janela de ouro (especialista #3)
TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.85"))
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "25"))
TOP_HOURS_COUNT       = int(os.getenv("TOP_HOURS_COUNT", "6"))

# Streak/ban (especialista #4)
BAN_AFTER_CONSECUTIVE_R = int(os.getenv("BAN_AFTER_CONSECUTIVE_R", "2"))
BAN_FOR_HOURS           = int(os.getenv("BAN_FOR_HOURS", "4"))

# Risco geral (especialista #5)
DAILY_STOP_LOSS   = int(os.getenv("DAILY_STOP_LOSS", "3"))
STREAK_GUARD_LOSSES = int(os.getenv("STREAK_GUARD_LOSSES", "2"))
COOLDOWN_MINUTES  = int(os.getenv("COOLDOWN_MINUTES", "30"))
HOURLY_CAP        = int(os.getenv("HOURLY_CAP", "8"))  # só avisa (soft)

# Fluxo
MIN_GAP_SECS = int(os.getenv("MIN_GAP_SECS", "12"))     # anti-spam leve

# Toggles
FLOW_THROUGH     = os.getenv("FLOW_THROUGH", "0") == "1"   # espelha tudo (debug)
LOG_RAW          = os.getenv("LOG_RAW", "1") == "1"
DISABLE_WINDOWS  = os.getenv("DISABLE_WINDOWS", "0") == "1"
DISABLE_RISK     = os.getenv("DISABLE_RISK", "0") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ============= APP/LOG =============
app = FastAPI()
log = logging.getLogger("uvicorn.error")

# ============= REGRAS DE TEXTO =============
PATTERN_RE = re.compile(r"(banker|player|empate|bac\s*bo|dados|entrada\s*confirmada|odd|even)", re.I)
NOISE_RE   = re.compile(r"(bot\s*online|estamos\s+no\s+\d+º?\s*gale|aposta\s*encerrada|analisando)", re.I)
GREEN_RE   = re.compile(r"(green|win|✅)", re.I)
RED_RE     = re.compile(r"(red|lose|perd|loss|derrota|❌)", re.I)

def now_local(): return datetime.now(LOCAL_TZ)
def today_str(): return now_local().strftime("%Y-%m-%d")
def hour_str(dt=None): return (dt or now_local()).strftime("%H")
def hour_key(dt=None): return (dt or now_local()).strftime("%Y-%m-%d %H")

def extract_pattern(text: str) -> str|None:
    f = PATTERN_RE.findall(text or "")
    if not f: return None
    return "+".join(sorted([w.strip().lower() for w in f]))

def colorize_line(text: str) -> str:
    t = text
    # ordem importa para não re-colorir
    t = re.sub(r"\bplayer\b",  "🔵 Player", t, flags=re.I)
    t = re.sub(r"\bempate\b",  "🟡 Empate", t, flags=re.I)
    t = re.sub(r"\bbanker\b",  "🔴 Banker", t, flags=re.I)
    return t

# ============= STATE =============
STATE = {
    "messages": [],                     # histórico leve
    "last_reset_date": None,

    "pattern_roll": {},                 # pattern -> deque("G"/"R")
    "recent_results": deque(maxlen=TREND_LOOKBACK),  # últimos resultados aprovados (G/R)

    "hour_stats": {},                   # "00".."23" -> {"g":int,"t":int}
    "pattern_ban": {},                  # pattern -> {"until": iso, "reason": str}

    "daily_losses": 0,
    "hourly_entries": {},               # "YYYY-MM-DD HH" -> int
    "cooldown_until": None,
    "recent_g0": [],

    "open_signal": None,                # {"ts","text","pattern"}
    "totals": {"greens": 0, "reds": 0},
    "streak_green": 0,

    "last_publish_ts": None,            # anti-spam
    "processed_updates": deque(maxlen=500),  # update_id dedup
    "last_summary_hash": None,          # anti duplicidade de resumo
}

def _ensure_dir(path):
    d = os.path.dirname(path)
    if d and not os.path.exists(d): os.makedirs(d, exist_ok=True)

def save_state():
    _ensure_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(STATE, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)

def load_state():
    global STATE
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f: STATE = json.load(f)
    except Exception:
        save_state()
load_state()

# ============= AUX LOG ============
async def aux_log_history(entry: dict):
    STATE["messages"].append(entry)
    if len(STATE["messages"]) > 5000:
        STATE["messages"] = STATE["messages"][-4000:]
    save_state()

# ============= MÉTRICAS ============
def rolling_append(pattern: str, result: str):
    dq = STATE["pattern_roll"].setdefault(pattern, deque(maxlen=ROLLING_MAX))
    dq.append(result)
    h = hour_str()
    hst = STATE["hour_stats"].setdefault(h, {"g": 0, "t": 0})
    hst["t"] += 1
    if result == "G": hst["g"] += 1

def rolling_wr(pattern: str) -> float:
    dq = STATE["pattern_roll"].get(pattern)
    if not dq: return 0.0
    return sum(1 for x in dq if x == "G") / len(dq)

def pattern_recent_tail(pattern: str, k: int) -> str:
    dq = STATE["pattern_roll"].get(pattern) or []
    tail = list(dq)[-k:]
    return "".join(tail)

def ban_pattern(pattern: str, reason: str):
    until = now_local() + timedelta(hours=BAN_FOR_HOURS)
    STATE["pattern_ban"][pattern] = {"until": until.isoformat(), "reason": reason}
    save_state()

def is_banned(pattern: str) -> bool:
    b = STATE["pattern_ban"].get(pattern)
    if not b: return False
    return datetime.fromisoformat(b["until"]) > now_local()

def cleanup_expired_bans():
    expired = [p for p,b in STATE["pattern_ban"].items()
               if datetime.fromisoformat(b["until"]) <= now_local()]
    for p in expired: STATE["pattern_ban"].pop(p, None)

def daily_reset_if_needed():
    if STATE.get("last_reset_date") != today_str():
        STATE["last_reset_date"] = today_str()
        STATE["daily_losses"] = 0
        STATE["hourly_entries"] = {}
        STATE["cooldown_until"] = None
        STATE["totals"] = {"greens": 0, "reds": 0}
        STATE["streak_green"] = 0
        STATE["open_signal"] = None
        cleanup_expired_bans()
        save_state()

# ============= RISCO / FLUXO ============
def cooldown_active():
    cu = STATE.get("cooldown_until")
    return bool(cu) and datetime.fromisoformat(cu) > now_local()

def start_cooldown():
    STATE["cooldown_until"] = (now_local() + timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
    save_state()

def streak_guard_triggered():
    k = STREAK_GUARD_LOSSES
    r = STATE.get("recent_g0", [])
    return k > 0 and len(r) >= k and all(x == "R" for x in r[-k:])

def hourly_cap_ok(): return STATE["hourly_entries"].get(hour_key(), 0) < HOURLY_CAP
def register_hourly_entry():
    k = hour_key()
    STATE["hourly_entries"][k] = STATE["hourly_entries"].get(k, 0) + 1
    save_state()

def is_top_hour_now() -> bool:
    if DISABLE_WINDOWS: return True
    stats = STATE.get("hour_stats", {})
    items = [(h,s) for h,s in stats.items() if s["t"] >= TOP_HOURS_MIN_SAMPLES]
    if not items: return True
    ranked = sorted(items, key=lambda kv: (kv[1]["g"]/kv[1]["t"]), reverse=True)
    top = []
    for h,s in ranked:
        wr = s["g"]/s["t"]
        if wr >= TOP_HOURS_MIN_WINRATE: top.append(h)
        if len(top) >= TOP_HOURS_COUNT: break
    return hour_str() in top if top else True

# ============= ESPECIALISTAS ============
def g0_allows(pattern: str) -> tuple[bool, float, int]:
    dq = STATE["pattern_roll"].get(pattern, deque())
    samples = len(dq)
    if samples < MIN_SAMPLES_BEFORE_FILTER:
        return True, 1.0, samples
    wr = rolling_wr(pattern)
    return (wr >= MIN_G0), wr, samples

def trend_allows() -> tuple[bool, float, int]:
    dq = list(STATE["recent_results"])
    n = len(dq)
    if n < max(10, int(TREND_LOOKBACK/2)):
        return True, 1.0, n  # aquecimento
    wr = sum(1 for x in dq if x == "G")/n
    return (wr >= TREND_MIN_WR), wr, n

def window_allows() -> tuple[bool, float]:
    ok = is_top_hour_now()
    return ok, 1.0 if ok else 0.0

def streak_ban_allows(pattern: str) -> tuple[bool, str]:
    if is_banned(pattern):
        return False, "pattern_banned"
    tail = pattern_recent_tail(pattern, BAN_AFTER_CONSECUTIVE_R)
    if tail and tail.endswith("R"*BAN_AFTER_CONSECUTIVE_R):
        ban_pattern(pattern, f"{BAN_AFTER_CONSECUTIVE_R} REDS seguidos")
        return False, "tail_rr"
    return True, ""

def risk_allows() -> tuple[bool, str]:
    if DISABLE_RISK: return True, ""
    if STATE["daily_losses"] >= DAILY_STOP_LOSS:
        return False, "stop_daily"
    if cooldown_active():
        return False, "cooldown"
    if streak_guard_triggered():
        start_cooldown()
        return False, "streak_guard"
    return True, ""

# ============= TELEGRAM ============
async def tg_send(chat_id: int, text: str, disable_preview=True):
    async with httpx.AsyncClient(timeout=20) as cli:
        await cli.post(f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": disable_preview})

# ============= PROCESSAMENTO ============
async def process_signal(text: str):
    daily_reset_if_needed()

    low = (text or "").lower()
    if NOISE_RE.search(low):
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"noise","text":text}))
        return

    pattern = extract_pattern(text)
    gatilho = ("entrada confirmada" in low) or re.search(r"\b(banker|player|empate|bac\s*bo|dados)\b", low)
    if not gatilho:
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"no_trigger","text":text}))
        return
    if not pattern and "entrada confirmada" in low:
        pattern = "fantan"

    # Especialista 4: streak/ban
    ok, why = streak_ban_allows(pattern)
    if not ok:
        log.info("DESCARTADO (ban/streak): %s", why)
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":why,"pattern":pattern,"text":text}))
        return

    # Especialista 1: G0 rolling
    allow_g0, wr_g0, n_g0 = g0_allows(pattern)
    if not allow_g0:
        log.info("REPROVADO G0: wr=%.2f am=%d", wr_g0, n_g0)
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"rejected_g0","wr":wr_g0,"n":n_g0,"pattern":pattern,"text":text}))
        return

    # Especialista 2: tendência
    allow_trend, wr_trend, n_trend = trend_allows()
    if not allow_trend:
        log.info("REPROVADO tendência: wr=%.2f n=%d", wr_trend, n_trend)
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"rejected_trend","wr":wr_trend,"n":n_trend,"text":text}))
        return

    # Especialista 3: janela de ouro (soft – se reprovar ainda segue, mas com tag)
    allow_window, _ = window_allows()
    window_tag = "" if allow_window else " <i>(fora da janela de ouro)</i>"

    # Especialista 5: risco global
    allow_risk, why_risk = risk_allows()
    if not allow_risk:
        log.info("BLOQUEADO por risco: %s", why_risk)
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":why_risk,"text":text}))
        return

    # Anti-spam leve
    last = STATE.get("last_publish_ts")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (now_local() - last_dt).total_seconds() < MIN_GAP_SECS:
                asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"anti_spam_gap","gap":MIN_GAP_SECS,"text":text}))
                return
        except Exception:
            pass

    # Publica
    pretty = colorize_line(text)
    STATE["open_signal"] = {"ts": now_local().isoformat(), "text": pretty, "pattern": pattern}
    STATE["last_publish_ts"] = now_local().isoformat()
    save_state()

    msg = (
        f"🚀 <b>ENTRADA ABERTA (G0)</b>\n"
        f"✅ G0 {wr_g0*100:.1f}% ({n_g0} am.) • Tendência {wr_trend*100:.1f}% ({n_trend}){window_tag}\n"
        f"{pretty}"
    )
    await tg_send(TARGET_CHAT_ID, msg)
    register_hourly_entry()

async def process_result(text: str):
    patt_hint = extract_pattern(text)
    is_green = bool(GREEN_RE.search(text))
    is_red   = bool(RED_RE.search(text))
    if not (is_green or is_red):
        return

    res = "G" if is_green else "R"
    if patt_hint:
        rolling_append(patt_hint, res)

    # alimentar tendência global (apenas quando houve entrada aberta recentemente)
    STATE["recent_results"].append(res)

    STATE["recent_g0"].append(res)
    STATE["recent_g0"] = STATE["recent_g0"][-10:]
    if res == "R":
        STATE["daily_losses"] = STATE.get("daily_losses", 0) + 1

    # contagem real
    if res == "G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] = STATE.get("streak_green", 0) + 1
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0

    # ban por sequência
    if patt_hint:
        tail = pattern_recent_tail(patt_hint, BAN_AFTER_CONSECUTIVE_R)
        if tail and tail.endswith("R"*BAN_AFTER_CONSECUTIVE_R):
            ban_pattern(patt_hint, f"{BAN_AFTER_CONSECUTIVE_R} REDS seguidos")

    # resumo (dedup firme)
    g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]
    total = g + r
    wr_day = (g/total*100.0) if total else 0.0
    resumo = f"✅ {g} ⛔️ {r} 🎯 Acertamos {wr_day:.2f}%\n🥇 ESTAMOS A {STATE['streak_green']} GREENS SEGUIDOS ⏳"

    # Evitar duplicar mesmo resumo
    digest = hashlib.md5(resumo.encode("utf-8")).hexdigest()
    if digest != STATE.get("last_summary_hash"):
        await tg_send(TARGET_CHAT_ID, resumo)
        STATE["last_summary_hash"] = digest

    # fechar operação (libera próxima)
    STATE["open_signal"] = None

    asyncio.create_task(aux_log_history({
        "ts": now_local().isoformat(),
        "type": "result_green" if is_green else "result_red",
        "pattern": patt_hint, "text": text
    }))
    save_state()

# ============= RELATÓRIO ============
def build_report():
    g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]
    total = g + r
    wr_day = (g/total*100.0) if total else 0.0

    rows = []
    for p, dq in STATE.get("pattern_roll", {}).items():
        n = len(dq)
        if n >= 10:
            wr = sum(1 for x in dq if x=="G")/n