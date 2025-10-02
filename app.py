# app.py — g0-pipeline (rigid 95/40 + 5 especialistas) + destravador FORCE_TRIGGER_OPEN + override opcional
import os, json, asyncio, re, pytz, hashlib
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
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
MIN_G0 = float(os.getenv("MIN_G0", "0.95"))                   # corte do G0 (rolling) — default rígido 95%
MIN_SAMPLES_BEFORE_FILTER = int(os.getenv("MIN_SAMPLES_BEFORE_FILTER", "40"))  # amostras mínimas
ROLLING_MAX = int(os.getenv("ROLLING_MAX", "600"))

# Tendência recente (especialista #2)
TREND_LOOKBACK = int(os.getenv("TREND_LOOKBACK", "40"))       # últimas N entradas aprovadas
TREND_MIN_WR   = float(os.getenv("TREND_MIN_WR", "0.85"))     # WR mínimo no lookback (leve)

# Janela de ouro (especialista #3)
TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.88"))
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "25"))
TOP_HOURS_COUNT       = int(os.getenv("TOP_HOURS_COUNT", "6"))

# Streak/ban (especialista #4)
BAN_AFTER_CONSECUTIVE_R = int(os.getenv("BAN_AFTER_CONSECUTIVE_R", "1"))  # 1 red já bane temporariamente
BAN_FOR_HOURS           = int(os.getenv("BAN_FOR_HOURS", "4"))

# Risco geral (especialista #5)
DAILY_STOP_LOSS   = int(os.getenv("DAILY_STOP_LOSS", "3"))
STREAK_GUARD_LOSSES = int(os.getenv("STREAK_GUARD_LOSSES", "1"))  # perdeu a última → cooldown
COOLDOWN_MINUTES  = int(os.getenv("COOLDOWN_MINUTES", "30"))
HOURLY_CAP        = int(os.getenv("HOURLY_CAP", "8"))  # aviso (soft)

# Fluxo
MIN_GAP_SECS = int(os.getenv("MIN_GAP_SECS", "12"))     # anti-spam leve

# Toggles
FLOW_THROUGH     = os.getenv("FLOW_THROUGH", "0") == "1"   # espelha tudo (debug)
LOG_RAW          = os.getenv("LOG_RAW", "1") == "1"
DISABLE_WINDOWS  = os.getenv("DISABLE_WINDOWS", "0") == "1"
DISABLE_RISK     = os.getenv("DISABLE_RISK", "0") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# === Override / decisão própria (opcional) ===
DECISION_STRATEGY = os.getenv("DECISION_STRATEGY", "").lower()  # "override" para ativar
P_MIN_OVERRIDE = float(os.getenv("P_MIN_OVERRIDE", "0.62"))
MARGEM_MIN     = float(os.getenv("MARGEM_MIN", "0.08"))
W_WR        = float(os.getenv("W_WR", "0.70"))
W_HOUR      = float(os.getenv("W_HOUR", "0.15"))
W_TREND     = float(os.getenv("W_TREND", "0.00"))
SOURCE_BIAS = float(os.getenv("SOURCE_BIAS", "0.15"))
MIN_SIDE_SAMPLES = int(os.getenv("MIN_SIDE_SAMPLES", "40"))

# === Destravador de fluxo no gatilho (aproveitar todos os fluxos) ===
FORCE_TRIGGER_OPEN = os.getenv("FORCE_TRIGGER_OPEN", "0") == "1"

# ============= APP/LOG =============
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

# ============= REGRAS DE TEXTO =============
PATTERN_RE = re.compile(r"(banker|player|empate|bac\s*bo|dados|entrada\s*confirmada|odd|even)", re.I)
NOISE_RE   = re.compile(r"(bot\s*online|estamos\s+no\s+\d+º?\s*gale|aposta\s*encerrada|analisando)", re.I)
GREEN_RE   = re.compile(r"(green|win|✅)", re.I)
RED_RE     = re.compile(r"(red|lose|perd|loss|derrota|❌)", re.I)
SIDE_RE    = re.compile(r"\b(player|banker|empate)\b", re.I)

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
    t = re.sub(r"\bplayer\b",  "🔵 Player", t, flags=re.I)
    t = re.sub(r"\bempate\b",  "🟡 Empate", t, flags=re.I)
    t = re.sub(r"\bbanker\b",  "🔴 Banker", t, flags=re.I)
    return t

def extract_side(text: str) -> str|None:
    m = SIDE_RE.search(text or "")
    return m.group(1).lower() if m else None

# ============= STATE =============
STATE = {
    "messages": [], "last_reset_date": None,
    "pattern_roll": {}, "recent_results": deque(maxlen=TREND_LOOKBACK),
    "hour_stats": {}, "pattern_ban": {},
    "daily_losses": 0, "hourly_entries": {}, "cooldown_until": None,
    "recent_g0": [],
    "open_signal": None,                # {"ts","text","pattern","chosen_side"}
    "totals": {"greens": 0, "reds": 0},
    "streak_green": 0,
    "last_publish_ts": None,
    "processed_updates": deque(maxlen=500),
    "last_summary_hash": None,
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

# ============= OVERRIDE (scoring) ============
def side_wr(side: str) -> tuple[float, int]:
    dq = STATE["pattern_roll"].get(side, deque())
    n = len(dq)
    if n == 0:
        return 0.0, 0
    wr = sum(1 for x in dq if x == "G") / n
    return wr, n

def hour_bonus() -> float:
    return 1.0 if is_top_hour_now() else 0.0

def trend_bonus() -> float:
    dq = list(STATE["recent_results"])
    n = len(dq)
    if n == 0:
        return 0.0
    wr = sum(1 for x in dq if x == "G") / n
    return max(0.0, min(1.0, wr))

def compute_override(text: str) -> tuple[str|None, dict]:
    fonte = extract_side(text)  # player/banker/empate/None
    if fonte in (None, "empate"):
        return None, {"reason": "no_override_on_empate_or_none"}

    wr_player, n_player = side_wr("player")
    wr_banker, n_banker = side_wr("banker")

    if min(n_player, n_banker) < MIN_SIDE_SAMPLES:
        return None, {"reason": "insufficient_side_samples",
                      "n_player": n_player, "n_banker": n_banker}

    hb = hour_bonus()
    tb = trend_bonus()
    bias_player = SOURCE_BIAS if fonte == "player" else 0.0
    bias_banker = SOURCE_BIAS if fonte == "banker" else 0.0

    score_player = (W_WR * wr_player) + (W_HOUR * hb) + (W_TREND * tb) + bias_player
    score_banker = (W_WR * wr_banker) + (W_HOUR * hb) + (W_TREND * tb) + bias_banker
    score_player = max(0.0, min(1.0, score_player))
    score_banker = max(0.0, min(1.0, score_banker))

    if score_player >= score_banker:
        winner, loser = "player", "banker"
        s_win, s_lose = score_player, score_banker
    else:
        winner, loser = "banker", "player"
        s_win, s_lose = score_banker, score_player

    delta = s_win - s_lose
    passes = (s_win >= P_MIN_OVERRIDE) and (delta >= MARGEM_MIN)

    dbg = {
        "fonte": fonte,
        "wr_player": wr_player, "n_player": n_player,
        "wr_banker": wr_banker, "n_banker": n_banker,
        "hour_bonus": hb, "trend_bonus": tb,
        "score_player": score_player,
        "score_banker": score_banker,
        "winner": winner, "delta": delta,
        "passes": passes, "threshold": P_MIN_OVERRIDE, "margin": MARGEM_MIN,
    }
    return (winner if passes else None), dbg

def decide_side_from_text(text: str) -> str|None:
    fonte_side = extract_side(text)
    if DECISION_STRATEGY == "override":
        chosen, _ = compute_override(text)
        if chosen:
            return chosen
    if fonte_side in ("player","banker"):
        return fonte_side
    return None

# ============= TELEGRAM ============
# (tg_send já definido acima)

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
        pattern = "bacbo"

    # Anti-spam
    last = STATE.get("last_publish_ts")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (now_local() - last_dt).total_seconds() < MIN_GAP_SECS:
                asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"anti_spam_gap","gap":MIN_GAP_SECS,"text":text}))
                return
        except Exception:
            pass

    # Especialista 4
    ok, why = streak_ban_allows(pattern)
    if not ok and not FORCE_TRIGGER_OPEN:
        log.info("DESCARTADO (ban/streak): %s", why)
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":why,"pattern":pattern,"text":text}))
        return

    # Especialistas 1–3–5
    allow_g0, wr_g0, n_g0 = g0_allows(pattern)
    allow_trend, wr_trend, n_trend = trend_allows()
    allow_window, _ = window_allows()
    allow_risk, why_risk = risk_allows()

    if not FORCE_TRIGGER_OPEN:
        if (n_g0 < MIN_SAMPLES_BEFORE_FILTER) or (wr_g0 < MIN_G0):
            log.info("REPROVADO G0 (rigid): wr=%.2f am=%d", wr_g0, n_g0)
            asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"rejected_g0_strict","wr":wr_g0,"n":n_g0,"pattern":pattern,"text":text}))
            return
        if not allow_trend:
            log.info("REPROVADO tendência: wr=%.2f n=%d", wr_trend, n_trend)
            asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"rejected_trend","wr":wr_trend,"n":n_trend,"text":text}))
            return
        if not allow_risk:
            log.info("BLOQUEADO por risco: %s", why_risk)
            asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":why_risk,"text":text}))
            return

    # Decide lado final
    final_side = decide_side_from_text(text)
    if final_side in ("player","banker"):
        pattern = final_side  # rolling por lado

    window_tag = "" if allow_window else " <i>(fora da janela de ouro)</i>"
    pretty = colorize_line(text)
    if final_side == "player":
        pretty = f"🔵 Player\n{pretty}"
    elif final_side == "banker":
        pretty = f"🔴 Banker\n{pretty}"

    STATE["open_signal"] = {
        "ts": now_local().isoformat(),
        "text": pretty,
        "pattern": pattern,
        "chosen_side": final_side
    }
    STATE["last_publish_ts"] = now_local().isoformat()
    save_state()

    msg_wr_line = ""
    if allow_g0 or not FORCE_TRIGGER_OPEN:
        msg_wr_line = f"✅ G0 {wr_g0*100:.1f}% ({n_g0} am.) • Tendência {wr_trend*100:.1f}% ({n_trend})"

    msg = (
        f"🚀 <b>ENTRADA ABERTA (G0)</b>{window_tag}\n"
        f"{msg_wr_line}\n"
        f"{pretty}"
    )
    await tg_send(TARGET_CHAT_ID, msg)
    register_hourly_entry()

def rolling_append_result_side_hint(text: str, res: str):
    """Se o texto do resultado contiver o lado, conta no rolling do lado indicado."""
    s = extract_side(text)
    if s in ("player","banker","empate"):
        dq = STATE["pattern_roll"].setdefault(s, deque(maxlen=ROLLING_MAX))
        dq.append(res)

async def announce_outcome(result: str, chosen_side: str | None):
    big = "🟩🟩🟩 <b>GREEN</b> 🟩🟩🟩" if result == "G" else "🟥🟥🟥 <b>LOSS</b> 🟥🟥🟥"
    ns = {"player":"🔵 Player","banker":"🔴 Banker"}.get((chosen_side or "").lower(),"?")
    await tg_send(TARGET_CHAT_ID, f"{big}\nNossa: {ns}")
    g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]
    t = g + r; wr = (g/t*100.0) if t else 0.0
    await tg_send(TARGET_CHAT_ID, f"📊 <b>Placar Geral</b>\n✅ {g}   ⛔️ {r}\n🎯 {wr:.2f}%  •  🔥 Streak {STATE.get('streak_green',0)}")

async def process_result(text: str):
    patt_hint = extract_pattern(text)
    is_green = bool(GREEN_RE.search(text))
    is_red   = bool(RED_RE.search(text))
    if not (is_green or is_red):
        return

    res = "G" if is_green else "R"

    if patt_hint:
        rolling_append(patt_hint, res)
    else:
        rolling_append_result_side_hint(text, res)

    STATE["recent_results"].append(res)
    STATE["recent_g0"].append(res)
    STATE["recent_g0"] = STATE["recent_g0"][-10:]
    if res == "R":
        STATE["daily_losses"] = STATE.get("daily_losses", 0) + 1

    if res == "G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] = STATE.get("streak_green", 0) + 1
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0

    chosen_side = None
    if STATE.get("open_signal"):
        chosen_side = STATE["open_signal"].get("chosen_side")

    await announce_outcome(res, chosen_side)
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
            wr = sum(1 for x in dq if x == "G") / n
            rows.append((p, wr, n))
    rows.sort(key=lambda x: x[1], reverse=True)
    tops = rows[:5]

    cleanup_expired_bans()
    bans = []
    for p, b in STATE.get("pattern_ban", {}).items():
        try:
            until = datetime.fromisoformat(b["until"]).strftime("%d/%m %H:%M")
        except Exception:
            until = b.get("until", "?")
        bans.append(f"• {p} (até {until})")

    lines = [
        f"<b>📊 Relatório Diário ({today_str()})</b>",
        f"Dia: <b>{g}G / {r}R</b>  (WR: {wr_day:.1f}%)",
        f"Stop-loss: {DAILY_STOP_LOSS}  •  Perdas hoje: {STATE.get('daily_losses',0)}",
        "", "<b>Top padrões (rolling):</b>",
    ]
    if tops:
        lines += [f"• {p} → {wr*100:.1f}% ({n})" for p, wr, n in tops]
    else:
        lines.append("• Sem dados suficientes.")
    lines += ["", "<b>Padrões banidos:</b>"]
    lines += bans or ["• Nenhum ativo."]
    return "\n".join(lines)

# ============= LOOP DE RELATÓRIO =============
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

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(daily_report_loop())

# ============= ROTAS =============
@app.get("/")
async def root():
    return {"ok": True, "service": "g0-pipeline (rigid 95/40 + 5 especialistas + FORCE_TRIGGER_OPEN + override opcional)"}

@app.post("/webhook")
@app.post("/webhook/{secret}")
async def webhook(req: Request, secret: str|None=None):
    try:
        update = await req.json()
    except Exception:
        body = (await req.body())[:1000]
        if LOG_RAW: log.info("RAW(non-json): %r", body)
        return JSONResponse({"ok": True})

    if LOG_RAW: log.info("RAW UPDATE: %s", update)

    # DEDUP por update_id do Telegram
    up_id = update.get("update_id")
    if up_id is not None:
        if up_id in STATE["processed_updates"]:
            return JSONResponse({"ok": True})
        STATE["processed_updates"].append(up_id)

    msg = update.get("channel_post") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    if not text or chat_id != SOURCE_CHAT_ID:
        return JSONResponse({"ok": True})

    if FLOW_THROUGH:
        await tg_send(TARGET_CHAT_ID, colorize_line(text))
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(), "type":"mirror", "text": text}))
        return JSONResponse({"ok": True})

    low = text.lower()

    if GREEN_RE.search(text) or RED_RE.search(text):
        await process_result(text)
        return JSONResponse({"ok": True})

    if (("entrada confirmada" in low) or
        re.search(r"\b(banker|player|empate|bac\s*bo|dados)\b", low)):
        await process_signal(text)
        return JSONResponse({"ok": True})

    asyncio.create_task(aux_log_history({"ts": now_local().isoformat(), "type":"other", "text": text}))
    return JSONResponse({"ok": True})