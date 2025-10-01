# app.py — Sniper G0 AUTÔNOMO (abre G0 sozinho), corte por lado final, aprendizado G1/G2, override opcional, regex azul/vermelho/emoji, dedup, fix deque
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

# ---------- Limiares (destravado por padrão; aperte depois) ----------
MIN_G0 = float(os.getenv("MIN_G0", "0.92"))                     # corte rígido por lado
MIN_SAMPLES_BEFORE_FILTER = int(os.getenv("MIN_SAMPLES_BEFORE_FILTER", "20"))
ROLLING_MAX = int(os.getenv("ROLLING_MAX", "800"))

TREND_LOOKBACK = int(os.getenv("TREND_LOOKBACK", "40"))
TREND_MIN_WR   = float(os.getenv("TREND_MIN_WR", "0.80"))

TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.85"))
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "20"))
TOP_HOURS_COUNT       = int(os.getenv("TOP_HOURS_COUNT", "8"))

BAN_AFTER_CONSECUTIVE_R = int(os.getenv("BAN_AFTER_CONSECUTIVE_R", "1"))
BAN_FOR_HOURS           = int(os.getenv("BAN_FOR_HOURS", "4"))

DAILY_STOP_LOSS   = int(os.getenv("DAILY_STOP_LOSS", "999"))   # destravado
STREAK_GUARD_LOSSES = int(os.getenv("STREAK_GUARD_LOSSES", "0"))
COOLDOWN_MINUTES  = int(os.getenv("COOLDOWN_MINUTES", "15"))
HOURLY_CAP        = int(os.getenv("HOURLY_CAP", "20"))

MIN_GAP_SECS = int(os.getenv("MIN_GAP_SECS", "5"))

FLOW_THROUGH     = os.getenv("FLOW_THROUGH", "1") == "1"   # espelha p/ debug
LOG_RAW          = os.getenv("LOG_RAW", "1") == "1"
DISABLE_WINDOWS  = os.getenv("DISABLE_WINDOWS", "0") == "1"
DISABLE_RISK     = os.getenv("DISABLE_RISK", "1") == "1"

STRICT_TRIGGER   = os.getenv("STRICT_TRIGGER", "0") == "1"
COUNT_STRATEGY_G0_ONLY = os.getenv("COUNT_STRATEGY_G0_ONLY", "1") == "1"
DEDUP_TTL_MIN    = int(os.getenv("DEDUP_TTL_MIN", "0"))

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ---------- Override opcional ----------
DECISION_STRATEGY = os.getenv("DECISION_STRATEGY", "override").lower()  # ligado
P_MIN_OVERRIDE = float(os.getenv("P_MIN_OVERRIDE", "0.62"))
MARGEM_MIN     = float(os.getenv("MARGEM_MIN", "0.08"))
W_WR        = float(os.getenv("W_WR", "0.70"))
W_HOUR      = float(os.getenv("W_HOUR", "0.20"))
W_TREND     = float(os.getenv("W_TREND", "0.00"))
SOURCE_BIAS = float(os.getenv("SOURCE_BIAS", "0.15"))
MIN_SIDE_SAMPLES = int(os.getenv("MIN_SIDE_SAMPLES", "30"))

# ---------- AUTÔNOMO ----------
AUTONOMOUS = os.getenv("AUTONOMOUS", "1") == "1"            # LIGA o modo autônomo
AUTONOMOUS_INTERVAL_SEC = int(os.getenv("AUTONOMOUS_INTERVAL_SEC", "12"))  # a cada X s tenta abrir
AUTO_MIN_SIDE_SAMPLES   = int(os.getenv("AUTO_MIN_SIDE_SAMPLES", "20"))    # base mínima por lado
AUTO_MIN_SCORE          = float(os.getenv("AUTO_MIN_SCORE", "0.62"))       # escore mínimo do lado
AUTO_MIN_MARGIN         = float(os.getenv("AUTO_MIN_MARGIN", "0.08"))      # margem mínima
AUTO_RESULT_TIMEOUT_MIN = int(os.getenv("AUTO_RESULT_TIMEOUT_MIN", "6"))   # expirar se canal não reportar
AUTO_MAX_PARALLEL       = int(os.getenv("AUTO_MAX_PARALLEL", "1"))         # só 1 G0 aberto
AUTO_RESPECT_WINDOWS    = os.getenv("AUTO_RESPECT_WINDOWS", "1") == "1"    # respeitar janela de ouro

# ---------- TRACE ----------
TRACE_MODE = os.getenv("TRACE_MODE", "1") == "1"

# ============= APP/LOG =============
app = FastAPI()
log = logging.getLogger("uvicorn.error")

# ============= REGRAS DE TEXTO =============
SIDE_ALIASES_RE = re.compile(r"\b(player|banker|empate|p\b|b\b|azul|vermelho|blue|red)\b", re.I)
PATTERN_RE = re.compile(r"(banker|player|empate|azul|vermelho|blue|red|p\b|b\b|bac\s*bo|dados|entrada\s*confirmada|\(?\s*g0\s*\)?)", re.I)
NOISE_RE   = re.compile(r"(bot\s*online|estamos\s+no\s+\d+º?\s*gale|aposta\s*encerrada|analisando|\bg-?1\b|\bg-?2\b|\bgale\b|\bgal[eé]\b|\bg1\b|\bg2\b)", re.I)
GREEN_RE   = re.compile(r"(green|win|✅)", re.I)
RED_RE     = re.compile(r"(red|lose|perd|loss|derrota|❌)", re.I)
G1_HINT_RE = re.compile(r"\b(g-?1|gale\s*1|primeiro\s*gale|indo\s+pro\s*g1|vamos\s+pro\s*g1)\b", re.I)
G2_HINT_RE = re.compile(r"\b(g-?2|gale\s*2|segundo\s*gale|indo\s+pro\s*g2|vamos\s+pro\s*g2)\b", re.I)
# Emojis lado
EMOJI_PLAYER_RE = re.compile(r"(🔵|🟦)", re.U)
EMOJI_BANKER_RE = re.compile(r"(🔴|🟥)", re.U)

def now_local(): return datetime.now(LOCAL_TZ)
def today_str(): return now_local().strftime("%Y-%m-%d")
def hour_str(dt=None): return (dt or now_local()).strftime("%H")
def hour_key(dt=None): return (dt or now_local()).strftime("%Y-%m-%d %H")

def colorize_line(text: str) -> str:
    t = text
    t = re.sub(r"\b(player|p)\b",  "🔵 Player", t, flags=re.I)
    t = re.sub(r"\b(azul|blue)\b", "🔵 Player", t, flags=re.I)
    t = re.sub(r"\b(banker|b)\b",  "🔴 Banker", t, flags=re.I)
    t = re.sub(r"\b(vermelho|red)\b","🔴 Banker", t, flags=re.I)
    t = re.sub(r"\bempate\b",      "🟡 Empate", t, flags=re.I)
    t = re.sub(r"\( ?g0 ?\)",      "(G0)", t, flags=re.I)
    return t

def normalize_side(token: str|None) -> str|None:
    if not token: return None
    t = token.lower()
    if t in ("player","p","azul","blue"): return "player"
    if t in ("banker","b","vermelho","red"): return "banker"
    if t == "empate": return "empate"
    return None

def extract_side(text: str) -> str|None:
    m = SIDE_ALIASES_RE.search(text or "")
    if m:
        side = normalize_side(m.group(0))
        if side: return side
    if EMOJI_PLAYER_RE.search(text or ""): return "player"
    if EMOJI_BANKER_RE.search(text or ""): return "banker"
    return None

def extract_pattern(text: str) -> str|None:
    f = PATTERN_RE.findall(text or "")
    if not f: return None
    return "+".join(sorted([w.strip().lower() for w in f]))

# ============= STATE =============
STATE = {
    "messages": [],
    "last_reset_date": None,

    "pattern_roll": {},                 # "player"/"banker" -> deque("G"/"R")
    "recent_results": deque(maxlen=TREND_LOOKBACK),

    "hour_stats": {},                   # "00".."23" -> {"g":int,"t":int}
    "pattern_ban": {},

    "daily_losses": 0,
    "hourly_entries": {},
    "cooldown_until": None,
    "recent_g0": [],

    "open_signal": None,                # {"ts","text","pattern","fonte_side","chosen_side","autonomous":bool,"expires_at":iso}
    "totals": {"greens": 0, "reds": 0},
    "streak_green": 0,

    "last_publish_ts": None,
    "processed_updates": deque(maxlen=500),
    "last_summary_hash": None,

    "recent_signal_hashes": {},
}

def _ensure_dir(path):
    d = os.path.dirname(path)
    if d and not os.path.exists(d): os.makedirs(d, exist_ok=True)

# -------- JSON save/load com fix deque --------
def save_state():
    def _convert(obj):
        from collections import deque as _dq
        if isinstance(obj, _dq):
            return list(obj)
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_convert(x) for x in obj]
        return obj
    _ensure_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    safe_state = _convert(STATE)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(safe_state, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)

def load_state():
    global STATE
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        pr = data.get("pattern_roll") or {}
        for p, lst in list(pr.items()):
            try: pr[p] = deque(lst, maxlen=ROLLING_MAX)
            except Exception: pr[p] = deque(maxlen=ROLLING_MAX)
        data["pattern_roll"] = pr
        rr = data.get("recent_results")
        data["recent_results"] = deque(rr or [], maxlen=TREND_LOOKBACK)
        rg0 = data.get("recent_g0") or []
        data["recent_g0"] = list(rg0)[-10:]
        data.setdefault("hourly_entries", {})
        data.setdefault("pattern_ban", {})
        data.setdefault("messages", [])
        data.setdefault("totals", {"greens": 0, "reds": 0})
        data.setdefault("recent_signal_hashes", {})
        STATE = data
    except Exception:
        save_state()
load_state()
# ----------------------------------------------

# ============= AUX LOG / TRACE / DEDUP ============
async def tg_send(chat_id: int, text: str, disable_preview=True):
    async with httpx.AsyncClient(timeout=20) as cli:
        await cli.post(f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": disable_preview})

async def trace(msg: str):
    if TRACE_MODE:
        try: await tg_send(TARGET_CHAT_ID, f"🧪 <b>TRACE</b> {msg}")
        except Exception: pass

def _soft_hash(s: str) -> str:
    t = re.sub(r"\s+", " ", (s or "").lower()).strip()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()

def dedup_recent_signal(text: str) -> bool:
    if DEDUP_TTL_MIN <= 0: return False
    h = _soft_hash(text)
    now = now_local()
    for k, v in list(STATE["recent_signal_hashes"].items()):
        if (now - datetime.fromisoformat(v)).total_seconds() > DEDUP_TTL_MIN * 60:
            STATE["recent_signal_hashes"].pop(k, None)
    if h in STATE["recent_signal_hashes"]:
        return True
    STATE["recent_signal_hashes"][h] = now.isoformat()
    save_state()
    return False

# ============= MÉTRICAS / RISCO / JANELA ============
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

def g0_side_allows(side: str) -> tuple[bool, float, int]:
    """Corte por LADO com warm-up: se n < MIN_SAMPLES, permite; senão exige wr >= MIN_G0."""
    if side not in ("player", "banker"): return False, 0.0, 0
    dq = STATE["pattern_roll"].get(side, deque()); n = len(dq)
    if n < MIN_SAMPLES_BEFORE_FILTER: return True, 0.0, n
    wr = sum(1 for x in dq if x == "G") / n if n else 0.0
    return (wr >= MIN_G0), wr, n

def trend_allows() -> tuple[bool, float, int]:
    dq = list(STATE["recent_results"]); n = len(dq)
    if n < max(10, int(TREND_LOOKBACK/2)): return True, 1.0, n
    wr = sum(1 for x in dq if x == "G")/n
    return (wr >= TREND_MIN_WR), wr, n

def window_allows() -> tuple[bool, float]:
    ok = is_top_hour_now()
    return ok, 1.0 if ok else 0.0

def streak_ban_allows(pattern: str) -> tuple[bool, str]:
    if is_banned(pattern): return False, "pattern_banned"
    tail = pattern_recent_tail(pattern, BAN_AFTER_CONSECUTIVE_R)
    if tail and tail.endswith("R"*BAN_AFTER_CONSECUTIVE_R):
        ban_pattern(pattern, f"{BAN_AFTER_CONSECUTIVE_R} REDS seguidos"); return False, "tail_rr"
    return True, ""

def risk_allows() -> tuple[bool, str]:
    if DISABLE_RISK: return True, ""
    if STATE["daily_losses"] >= DAILY_STOP_LOSS: return False, "stop_daily"
    if cooldown_active(): return False, "cooldown"
    if streak_guard_triggered():
        start_cooldown(); return False, "streak_guard"
    return True, ""

# ============= SCORING (override e autônomo) ============
def side_wr(side: str) -> tuple[float, int]:
    dq = STATE["pattern_roll"].get(side, deque()); n = len(dq)
    if n == 0: return 0.0, 0
    wr = sum(1 for x in dq if x == "G") / n
    return wr, n

def hour_bonus() -> float: return 1.0 if is_top_hour_now() else 0.0

def trend_bonus() -> float:
    dq = list(STATE["recent_results"]); n = len(dq)
    if n == 0: return 0.0
    wr = sum(1 for x in dq if x == "G") / n
    return max(0.0, min(1.0, wr))

def compute_scores(fonte_side: str|None) -> tuple[str, float, float, dict]:
    wr_p, n_p = side_wr("player")
    wr_b, n_b = side_wr("banker")
    hb = hour_bonus(); tb = trend_bonus()

    bias_p = SOURCE_BIAS if fonte_side == "player" else 0.0
    bias_b = SOURCE_BIAS if fonte_side == "banker" else 0.0

    s_p = max(0.0, min(1.0, (W_WR*wr_p) + (W_HOUR*hb) + (W_TREND*tb) + bias_p))
    s_b = max(0.0, min(1.0, (W_WR*wr_b) + (W_HOUR*hb) + (W_TREND*tb) + bias_b))

    winner, s_win, s_lose = ("player", s_p, s_b) if s_p >= s_b else ("banker", s_b, s_p)
    delta = s_win - s_lose
    dbg = {
        "wr_player": wr_p, "n_player": n_p, "wr_banker": wr_b, "n_banker": n_b,
        "hour_bonus": hb, "trend_bonus": tb, "score_player": s_p, "score_banker": s_b, "delta": delta
    }
    return winner, s_win, delta, dbg

def compute_override(text: str) -> tuple[str|None, dict]:
    fonte = extract_side(text)
    if fonte in (None, "empate"):
        return None, {"reason": "no_override_on_empate_or_none"}
    winner, s_win, delta, dbg = compute_scores(fonte)
    passes = (s_win >= P_MIN_OVERRIDE) and (delta >= MARGEM_MIN) and (min(dbg["n_player"], dbg["n_banker"]) >= MIN_SIDE_SAMPLES)
    dbg["fonte"] = fonte; dbg["passes"] = passes; dbg["threshold"] = P_MIN_OVERRIDE; dbg["margin"] = MARGEM_MIN
    return (winner if passes else None), dbg

# ============= ABERTURA DE SINAL (CANAL / AUTÔNOMO) ============
async def publish_entry(final_side: str, fonte_side: str|None, header: str, extra_line: str=""):
    pretty = header
    dir_txt = "🔵 Player" if final_side == "player" else "🔴 Banker"
    pretty = f"{dir_txt}\n{pretty}"

    # corte (info final para o texto)
    allow_g0, wr_g0, n_g0 = g0_side_allows(final_side or "")
    window_ok, _ = window_allows()
    window_tag = "" if window_ok else " <i>(fora da janela de ouro)</i>"

    msg = (
        f"🚀 <b>{'ENTRADA AUTÔNOMA' if fonte_side is None else 'ENTRADA ABERTA (G0)'}</b>\n"
        f"✅ G0 {wr_g0*100:.1f}% ({n_g0} am.){window_tag}{extra_line}\n"
        f"{pretty}"
    )
    await tg_send(TARGET_CHAT_ID, msg)
    register_hourly_entry()

# ============= PROCESSAMENTO DE SINAL DO CANAL ============
async def process_signal(text: str):
    daily_reset_if_needed()

    low = (text or "").lower()
    if NOISE_RE.search(low):
        await trace("noise/ignorado"); 
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"noise","text":text}))
        return

    # Gatilho do canal (se quiser aceitar também lado direto)
    if STRICT_TRIGGER:
        gatilho = ("entrada confirmada" in low) or re.search(r"\b\(?\s*g0\s*\)?\b", low)
    else:
        gatilho = ("entrada confirmada" in low) or re.search(r"\b\(?\s*g0\s*\)?\b", low) or (extract_side(text) in ("player","banker"))

    if not gatilho:
        await trace("sem gatilho G0 (canal)"); 
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"no_trigger","text":text}))
        return

    if dedup_recent_signal(text):
        await trace("dedup: sinal repetido")
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"dupe_signal","text":text}))
        return

    # risco / janela / tendência
    ok, why = streak_ban_allows("g0")
    if not ok: await trace(f"ban/streak: {why}"); return

    allow_trend, wr_trend, n_trend = trend_allows()
    if not allow_trend: await trace(f"reprovado tendência wr={wr_trend:.2f} n={n_trend}"); return

    allow_window, _ = window_allows()
    if not allow_window: await trace("fora da janela de ouro (canal)")

    allow_risk, why_risk = risk_allows()
    if not allow_risk: await trace(f"risco bloqueou: {why_risk}"); return

    # anti-spam
    last = STATE.get("last_publish_ts")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (now_local() - last_dt).total_seconds() < MIN_GAP_SECS:
                await trace(f"anti_spam_gap < {MIN_GAP_SECS}s"); return
        except Exception: pass

    # override (opcional)
    fonte_side = extract_side(text)  # pode vir None
    chosen_side = None; dbg_override = {}; override_tag = ""
    if DECISION_STRATEGY == "override":
        chosen_side, dbg_override = compute_override(text)
        if chosen_side and fonte_side not in (None, "empate") and chosen_side != fonte_side:
            override_tag = " <i>(override ao fonte)</i>"

    final_side = (chosen_side or fonte_side)
    if final_side not in ("player","banker"):
        await trace("sem lado final (canal)"); return

    # corte por lado final
    allow_g0, wr_g0, n_g0 = g0_side_allows(final_side or "")
    if not allow_g0:
        await trace(f"corte lado_final={final_side} wr={wr_g0:.2f} n={n_g0} (cut {MIN_G0:.2f}/{MIN_SAMPLES_BEFORE_FILTER})")
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"rejected_g0_strict_side","final_side": final_side, "wr": wr_g0, "n": n_g0, "text": text}))
        return
    else:
        await trace(f"OK lado_final={final_side} wr={wr_g0:.2f} n={n_g0}")

    # publica
    header = colorize_line(text)
    if chosen_side:
        sp = dbg_override.get("score_player", 0.0); sb = dbg_override.get("score_banker", 0.0); delta = dbg_override.get("delta", 0.0)
        extra = f"\n<i>Override:</i> escP={sp:.2f} escB={sb:.2f} (Δ={delta:.2f}){override_tag}"
    else:
        extra = ""
    STATE["open_signal"] = {
        "ts": now_local().isoformat(),
        "text": header,
        "pattern": final_side,
        "fonte_side": fonte_side,
        "chosen_side": final_side,
        "autonomous": False,
        "expires_at": (now_local() + timedelta(minutes=AUTO_RESULT_TIMEOUT_MIN)).isoformat()
    }
    STATE["last_publish_ts"] = now_local().isoformat()
    save_state()

    await publish_entry(final_side, fonte_side, header, extra)

# ======== AUTÔNOMO: decide e publica sem depender do canal ========
async def autonomous_try_open():
    # já existe aberto?
    if STATE.get("open_signal"): 
        await trace("autônomo: já existe G0 aberto"); 
        return
    # paralelismo max
    if AUTO_MAX_PARALLEL <= 0: return

    # risco / cooldown / cap
    allow_risk, why = risk_allows()
    if not allow_risk: await trace(f"autônomo: risco bloqueou {why}"); return
    if not hourly_cap_ok(): await trace("autônomo: cap horário"); return

    # janela (se respeitar)
    if AUTO_RESPECT_WINDOWS and not is_top_hour_now():
        await trace("autônomo: fora da janela"); 
        return

    # base mínima
    wr_p, n_p = side_wr("player"); wr_b, n_b = side_wr("banker")
    if min(n_p, n_b) < AUTO_MIN_SIDE_SAMPLES:
        await trace(f"autônomo: base insuficiente nP={n_p} nB={n_b} (<{AUTO_MIN_SIDE_SAMPLES})"); 
        return

    # scores (sem fonte)
    winner, s_win, delta, dbg = compute_scores(None)
    if (s_win < AUTO_MIN_SCORE) or (delta < AUTO_MIN_MARGIN):
        await trace(f"autônomo: score baixo side={winner} s={s_win:.2f} Δ={delta:.2f} (min {AUTO_MIN_SCORE}/{AUTO_MIN_MARGIN})")
        return

    # corte por lado final (mesmo no autônomo)
    allow_g0, wr_g0, n_g0 = g0_side_allows(winner)
    if not allow_g0:
        await trace(f"autônomo: corte reprovado side={winner} wr={wr_g0:.2f} n={n_g0}")
        return

    # anti-spam
    last = STATE.get("last_publish_ts")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (now_local() - last_dt).total_seconds() < MIN_GAP_SECS:
                await trace(f"autônomo: anti_spam_gap < {MIN_GAP_SECS}s"); return
        except Exception: pass

    # publica autônomo
    header = f"(G0) entrada técnica por score — escP={dbg['score_player']:.2f} escB={dbg['score_banker']:.2f} (Δ={dbg['delta']:.2f})"
    STATE["open_signal"] = {
        "ts": now_local().isoformat(),
        "text": header,
        "pattern": winner,
        "fonte_side": None,           # sem fonte (autônomo)
        "chosen_side": winner,
        "autonomous": True,
        "expires_at": (now_local() + timedelta(minutes=AUTO_RESULT_TIMEOUT_MIN)).isoformat()
    }
    STATE["last_publish_ts"] = now_local().isoformat()
    save_state()

    await trace(f"autônomo: PUBLICA side={winner}")
    await publish_entry(winner, None, header, "")

# ========= Settlers / Resultados =========
async def settle_open_g0_due_to_gale(hint_text: str, reason: str = "canal_moveu_para_gale"):
    open_sig = STATE.get("open_signal")
    if not open_sig: return

    chosen = open_sig.get("chosen_side") or open_sig.get("pattern")  # player/banker
    fonte  = open_sig.get("fonte_side")  # None no autônomo

    # Se não há fonte (autônomo), gale do canal indica apenas que o lado do canal errou — não sabemos se nosso chosen coincide.
    # Estratégia: se fonte is None, consideramos gale/green/red do canal como resultado do MUNDO, e decidimos por chosen X canal:
    # - se canal sobe para G1/G2 e nosso chosen é OPOSTO ao que o canal estava marcando naquele momento, contamos G.
    # - sem saber lado do canal atual, conservador: ignora (não conta). Aqui ficaremos conservadores para gale sem fonte.
    if fonte not in ("player","banker"):
        # conservador: não altera rolling; só loga
        asyncio.create_task(aux_log_history({
            "ts": now_local().isoformat(),
            "type": "autonomous_gale_hint_ignored",
            "text": hint_text
        }))
        return

    res = "R" if (chosen == fonte) else "G"
    rolling_append(chosen, res)
    STATE["recent_results"].append(res)
    STATE["recent_g0"].append(res); STATE["recent_g0"] = STATE["recent_g0"][-10:]
    if res == "R": STATE["daily_losses"] = STATE.get("daily_losses", 0) + 1

    if COUNT_STRATEGY_G0_ONLY:
        if res == "G":
            STATE["totals"]["greens"] += 1; STATE["streak_green"] = STATE.get("streak_green", 0) + 1
        else:
            STATE["totals"]["reds"] += 1; STATE["streak_green"] = 0

    STATE["open_signal"] = None; save_state()
    asyncio.create_task(aux_log_history({
        "ts": now_local().isoformat(),
        "type": "g0_settled_by_gale",
        "reason": reason, "fonte_side": fonte, "chosen_side": chosen, "result": res, "text": hint_text
    }))

async def process_result(text: str):
    is_green = bool(GREEN_RE.search(text)); is_red = bool(RED_RE.search(text))
    if not (is_green or is_red): return

    open_sig = STATE.get("open_signal")
    if COUNT_STRATEGY_G0_ONLY and open_sig is None:
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type": "late_result_ignored_g0_only","text": text}))
        return

    res = "G" if is_green else "R"
    chosen = (open_sig.get("chosen_side") or open_sig.get("pattern")) if open_sig else None
    if chosen: rolling_append(chosen, res)
    STATE["recent_results"].append(res)
    STATE["recent_g0"].append(res); STATE["recent_g0"] = STATE["recent_g0"][-10:]
    if res == "R": STATE["daily_losses"] = STATE.get("daily_losses", 0) + 1

    if res == "G":
        STATE["totals"]["greens"] += 1; STATE["streak_green"] = STATE.get("streak_green", 0) + 1
    else:
        STATE["totals"]["reds"] += 1; STATE["streak_green"] = 0

    # ban por sequência
    if chosen:
        tail = pattern_recent_tail(chosen, BAN_AFTER_CONSECUTIVE_R)
        if tail and tail.endswith("R"*BAN_AFTER_CONSECUTIVE_R): ban_pattern(chosen, f"{BAN_AFTER_CONSECUTIVE_R} REDS seguidos")

    # resumo
    g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]; total = g + r
    wr_day = (g/total*100.0) if total else 0.0
    resumo = f"✅ {g} ⛔️ {r} 🎯 Acertamos {wr_day:.2f}%\n🥇 ESTAMOS A {STATE['streak_green']} GREENS SEGUIDOS ⏳"
    digest = hashlib.md5(resumo.encode("utf-8")).hexdigest()
    if digest != STATE.get("last_summary_hash"):
        await tg_send(TARGET_CHAT_ID, resumo); STATE["last_summary_hash"] = digest

    STATE["open_signal"] = None; save_state()
    asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type": "result", "result": res, "chosen": chosen, "text": text}))

# ======= EXPIRAÇÃO de G0 autônomo sem resultado =======
async def expire_open_if_needed():
    open_sig = STATE.get("open_signal")
    if not open_sig: return
    exp = open_sig.get("expires_at")
    if not exp: return
    if datetime.fromisoformat(exp) <= now_local():
        # expira sem contar (neutro)
        asyncio.create_task(aux_log_history({
            "ts": now_local().isoformat(),
            "type": "auto_expired_no_result",
            "chosen_side": open_sig.get("chosen_side"),
        }))
        STATE["open_signal"] = None; save_state()
        await trace("autônomo: expirada por timeout (sem resultado do canal)")

# ============= RELATÓRIO ============
def build_report():
    g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]; total = g + r
    wr_day = (g/total*100.0) if total else 0.0

    rows = []
    for p, dq in STATE.get("pattern_roll", {}).items():
        n = len(dq)
        if n >= 10:
            wr = sum(1 for x in dq if x == "G") / n
            rows.append((p, wr, n))
    rows.sort(key=lambda x: x[1], reverse=True)
    tops = rows[:5]

    cleanup_expired_bans()
    bans = []
    for p, b in STATE.get("pattern_ban", {}).items():
        try: until = datetime.fromisoformat(b["until"]).strftime("%d/%m %H:%M")
        except Exception: until = b.get("until", "?")
        bans.append(f"• {p} (até {until})")

    lines = [
        f"<b>📊 Relatório Diário ({today_str()})</b>",
        f"Dia: <b>{g}G / {r}R</b>  (WR: {wr_day:.1f}%)",
        f"Stop-loss: {DAILY_STOP_LOSS}  •  Perdas hoje: {STATE.get('daily_losses',0)}",
        "", "<b>Top lados (rolling G0):</b>",
    ]
    if tops:
        lines += [f"• {p} → {wr*100:.1f}% ({n})" for p, wr, n in tops]
    else:
        lines.append("• Sem dados suficientes.")
    lines += ["", "<b>Lados banidos:</b>"]
    lines += bans or ["• Nenhum ativo."]
    return "\n".join(lines)

# ============= LOOPS =============
async def daily_report_loop():
    await asyncio.sleep(5)
    while True:
        try:
            n = now_local()
            if n.hour == 0 and n.minute == 0:
                daily_reset_if_needed()
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

# ============= ROTAS =============
@app.get("/")
async def root():
    return {"ok": True, "service": "sniper-g0 AUTÔNOMO (rigid-by-side + gale learning + override + aliases azul/vermelho/emoji)"}

@app.post("/webhook")
@app.post("/webhook/{secret}")
async def webhook(req: Request, secret: str|None=None):
    update = await req.json()
    if LOG_RAW: log.info("RAW UPDATE: %s", update)

    # DEDUP por update_id
    up_id = update.get("update_id")
    if up_id is not None:
        if up_id in STATE["processed_updates"]:
            return {"ok": True}
        STATE["processed_updates"].append(up_id)

    msg = update.get("channel_post") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    if not text or chat_id != SOURCE_CHAT_ID:
        return {"ok": True}

    if FLOW_THROUGH:
        await tg_send(TARGET_CHAT_ID, colorize_line(text))
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(), "type":"mirror", "text": text}))

    # Fechamento por G1/G2
    if G1_HINT_RE.search(text) or G2_HINT_RE.search(text):
        await settle_open_g0_due_to_gale(text, reason="canal_moveu_para_gale")
        return {"ok": True}

    # Resultado explícito
    if GREEN_RE.search(text) or RED_RE.search(text):
        await process_result(text)
        return {"ok": True}

    # Gatilho do canal (opcional; autônomo independe disso)
    low = text.lower()
    gatilho_ok = (("entrada confirmada" in low) or re.search(r"\b\(?\s*g0\s*\)?\b", low)) if STRICT_TRIGGER else (
        ("entrada confirmada" in low) or re.search(r"\b\(?\s*g0\s*\)?\b", low) or (extract_side(text) in ("player","banker"))
    )
    if gatilho_ok:
        await process_signal(text)
        return {"ok": True}

    # Ruído
    asyncio.create_task(aux_log_history({"ts": now_local().isoformat(), "type":"other", "text": text}))
    return {"ok": True}