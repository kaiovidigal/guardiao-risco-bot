# app.py — pipeline G0 com dedupe + fechamento GREEN/LOSS
import os, json, asyncio, re, pytz
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
import httpx
import logging
from collections import deque

# ====== ENV ======
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])

TZ_NAME = os.getenv("TZ", "UTC")
LOCAL_TZ = pytz.timezone(TZ_NAME)

# Mais “exigente” por padrão (pode sobrescrever no Render)
MIN_G0 = float(os.getenv("MIN_G0", "0.90"))
MIN_SAMPLES_BEFORE_FILTER = int(os.getenv("MIN_SAMPLES_BEFORE_FILTER", "30"))
ROLLING_MAX = int(os.getenv("ROLLING_MAX", "500"))

BAN_AFTER_CONSECUTIVE_R = int(os.getenv("BAN_AFTER_CONSECUTIVE_R", "2"))
BAN_FOR_HOURS = int(os.getenv("BAN_FOR_HOURS", "4"))

DAILY_STOP_LOSS = int(os.getenv("DAILY_STOP_LOSS", "3"))
STREAK_GUARD_LOSSES = int(os.getenv("STREAK_GUARD_LOSSES", "2"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))
HOURLY_CAP = int(os.getenv("HOURLY_CAP", "6"))

TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.85"))
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "25"))
TOP_HOURS_COUNT = int(os.getenv("TOP_HOURS_COUNT", "6"))

# Toggles
FLOW_THROUGH   = os.getenv("FLOW_THROUGH", "0") == "1"  # espelhar tudo (teste)
LOG_RAW        = os.getenv("LOG_RAW", "0") == "1"
DISABLE_WINDOWS= os.getenv("DISABLE_WINDOWS", "0") == "1"
DISABLE_RISK   = os.getenv("DISABLE_RISK", "0") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ====== APP/LOG ======
app = FastAPI()
log = logging.getLogger("uvicorn.error")

# ====== REGEX ======
PATTERN_RE = re.compile(r"(banker|player|empate|bac\s*bo|dados|entrada\s*confirmada|odd|even)", re.I)
NOISE_RE   = re.compile(r"(bot\s*online|aposta\s*encerrada|analisando|estamos\s+no\s+\d+º?\s*gale)", re.I)

# resultados — inclui termos comuns de derrota em pt
GREEN_RE = re.compile(r"(?:\bgreen\b|\bwin\b|✅)", re.I)
RED_RE   = re.compile(r"(?:\bred\b|\blose\b|\bloss\b|\bderrota\b|\bperd(?:e|emos)\b|❌)", re.I)

def now_local(): return datetime.now(LOCAL_TZ)
def today_str(): return now_local().strftime("%Y-%m-%d")
def hour_str(dt=None): return (dt or now_local()).strftime("%H")
def hour_key(dt=None): return (dt or now_local()).strftime("%Y-%m-%d %H")

def extract_pattern(text: str) -> str|None:
    f = PATTERN_RE.findall(text or "")
    if not f: return None
    return "+".join(sorted([w.strip().lower() for w in f]))

# ====== STATE ======
STATE = {
    "messages": [],
    "last_reset_date": None,

    "pattern_roll": {},      # pattern -> deque("G"/"R")
    "hour_stats": {},        # "00".."23" -> {"g":int,"t":int}
    "pattern_ban": {},       # pattern -> {"until": ISO, "reason": str}

    "daily_losses": 0,
    "hourly_entries": {},
    "cooldown_until": None,
    "recent_g0": [],

    "open_signal": None,     # {"ts","text","pattern"}
    "totals": {"greens": 0, "reds": 0},
    "streak_green": 0,

    # dedupe por message_id do Telegram (canal origem)
    "seen_msg_ids": []       # list[int]
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

# ====== AUX LOG ======
async def aux_log_history(entry: dict):
    STATE["messages"].append(entry)
    if len(STATE["messages"]) > 5000:
        STATE["messages"] = STATE["messages"][-4000:]
    save_state()

# ====== ROLLING ======
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
    return "".join(list(dq)[-k:])

def ban_pattern(pattern: str, reason: str):
    until = now_local() + timedelta(hours=BAN_FOR_HOURS)
    STATE["pattern_ban"][pattern] = {"until": until.isoformat(), "reason": reason}
    save_state()

def is_banned(pattern: str) -> bool:
    b = STATE["pattern_ban"].get(pattern)
    return bool(b) and datetime.fromisoformat(b["until"]) > now_local()

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

# ====== RISCO / FLUXO ======
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

def hourly_cap_ok(): 
    return STATE["hourly_entries"].get(hour_key(), 0) < HOURLY_CAP

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

# ====== TELEGRAM ======
async def tg_send(chat_id: int, text: str, disable_preview=True):
    async with httpx.AsyncClient(timeout=20) as cli:
        await cli.post(f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": disable_preview})

# ====== G0 (gating) ======
def g0_allows(pattern: str) -> tuple[bool, float, int]:
    dq = STATE["pattern_roll"].get(pattern, deque())
    samples = len(dq)
    if samples < MIN_SAMPLES_BEFORE_FILTER:
        return True, 1.0, samples  # aquecimento
    wr = rolling_wr(pattern)
    return (wr >= MIN_G0), wr, samples

# ====== PROCESSAMENTO ======
async def process_signal(text: str):
    daily_reset_if_needed()

    if STATE.get("open_signal"):
        log.info("IGNORADO: já existe operação aberta")
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"ignored_open","text":text}))
        return

    low = (text or "").lower()
    if NOISE_RE.search(low):
        log.info("DESCARTADO: ruído")
        asyncio.create_task(aux_log_history({"ts