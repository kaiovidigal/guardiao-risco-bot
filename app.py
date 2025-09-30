# app.py — G0 EXIGENTE (gate G0 + estatística + fechamento automático)
import os, json, asyncio, re, math, pytz
from datetime import datetime, timedelta
from collections import deque
from fastapi import FastAPI, Request
import httpx
import logging

# ========= ENV =========
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])     # origem (canal fonte)
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])     # destino
TZ_NAME  = os.getenv("TZ", "UTC")
LOCAL_TZ = pytz.timezone(TZ_NAME)

# Gate G0 & estatística
G0_ONLY = os.getenv("G0_ONLY", "1") == "1"
MIN_G0 = float(os.getenv("MIN_G0", "0.88"))                  # alvo de WR global
MIN_SAMPLES_BEFORE_FILTER = int(os.getenv("MIN_SAMPLES_BEFORE_FILTER", "40"))
ROLLING_MAX = int(os.getenv("ROLLING_MAX", "500"))
WILSON_CONF = float(os.getenv("WILSON_CONF", "0.90"))        # 0.90 ou 0.95
WILSON_MIN  = float(os.getenv("WILSON_MIN", "0.85"))         # bound inferior exigido
RECENT_WINDOW = int(os.getenv("RECENT_WINDOW", "30"))
RECENT_MIN_WR = float(os.getenv("RECENT_MIN_WR", "0.85"))
BAN_AFTER_RED_STREAK = int(os.getenv("BAN_AFTER_RED_STREAK", "1"))
BAN_FOR_HOURS = int(os.getenv("BAN_FOR_HOURS", "4"))

# Risco / fluxo
DAILY_STOP_LOSS = int(os.getenv("DAILY_STOP_LOSS", "3"))
STREAK_GUARD_LOSSES = int(os.getenv("STREAK_GUARD_LOSSES", "1"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "20"))
HOURLY_CAP = int(os.getenv("HOURLY_CAP", "12"))

# Janelas de ouro
DISABLE_WINDOWS = os.getenv("DISABLE_WINDOWS", "0") == "1"
TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.88"))
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "30"))
TOP_HOURS_COUNT = int(os.getenv("TOP_HOURS_COUNT", "6"))

# Toggles
FLOW_THROUGH = os.getenv("FLOW_THROUGH", "0") == "1"   # espelha tudo (teste)
DISABLE_RISK = os.getenv("DISABLE_RISK", "0") == "1"
LOG_RAW      = os.getenv("LOG_RAW", "1") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI()
log = logging.getLogger("uvicorn.error")

# ========= REGRAS DE TEXTO =========
PATTERN_RE = re.compile(r"(banker|player|empate|bac\s*bo|dados|entrada\s*confirmada|odd|even)", re.I)
NOT_G0_RE  = re.compile(r"\b(G1|G2|gale|2(?:º|o)\s*gale|runner\s*up|ls2|martingale)\b", re.I)

GREEN_RE = re.compile(r"(green|win|✅|\bganho\b|\bbateu\s*green\b)", re.I)
RED_RE   = re.compile(r"(red|loss|lose|❌|\bperd(?:emos|a|eu)?\b|\bfoi\s*red\b|\bbateu\s*red\b|\bdeu\s*ruim\b|\bderrota\b)", re.I)

def now_local(): return datetime.now(LOCAL_TZ)
def today_str(): return now_local().strftime("%Y-%m-%d")
def hour_str(dt=None): return (dt or now_local()).strftime("%H")
def hour_key(dt=None): return (dt or now_local()).strftime("%Y-%m-%d %H")

def extract_pattern(text: str) -> str | None:
    f = PATTERN_RE.findall(text or "")
    if not f: return None
    return "+".join(sorted([w.strip().lower() for w in f]))

def g0_gate_text(text: str) -> bool:
    # sempre deixa passar mensagens de fechamento
    if GREEN_RE.search(text) or RED_RE.search(text):
        return True
    # barra gales/G1/G2 etc
    if G0_ONLY and NOT_G0_RE.search(text):
        return False
    # opcional: exige que o texto mencione G0
    if G0_ONLY and "G0" not in text.upper():
        return False
    return True

def wilson_lower_bound(greens: int, total: int, conf: float = 0.90) -> float:
    if total <= 0: return 0.0
    p = greens / total
    z = 1.645 if conf <= 0.90 else 1.96
    denom = 1 + (z*z)/total
    centre = p + (z*z)/(2*total)
    margin = z*math.sqrt((p*(1-p) + (z*z)/(4*total))/total)
    return (centre - margin)/denom

# ========= STATE =========
STATE = {
    "messages": [],           # [{ts,type,text,hour,pattern}]
    "last_reset_date": None,
    "pattern_roll": {},       # pattern -> deque("G"/"R", maxlen=ROLLING_MAX)
    "hour_stats": {},         # "00".."23" -> {"g":int,"t":int}
    "pattern_ban": {},        # pattern -> {"until": ISO, "reason": str}
    "daily_losses": 0,
    "hourly_entries": {},     # "YYYY-MM-DD HH" -> int
    "cooldown_until": None,
    "recent_g0": [],          # últimos G/R
    "open_signal": None,      # {"ts","text","pattern"}
    "totals": {"greens": 0, "reds": 0},
    "streak_green": 0,
    "seen_msg_ids": [],       # anti-duplicado (message_id)
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
    except Exception: save_state()
load_state()

# ========= MÉTRICAS =========
def rolling_append(pattern: str, result: str):
    dq = STATE["pattern_roll"].setdefault(pattern, deque(maxlen=ROLLING_MAX))
    dq.append(result)
    h = hour_str()
    hst = STATE["hour_stats"].setdefault(h, {"g": 0, "t": 0})
    hst["t"] += 1
    if result == "G": hst["g"] += 1

def rolling_wr(pattern: str) -> tuple[float, int]:
    dq = STATE["pattern_roll"].get(pattern)
    if not dq: return 0.0, 0
    n = len(dq); g = sum(1 for x in dq if x == "G")
    return (g/n, n)

def recent_wr(pattern: str, window: int) -> tuple[float,int]:
    msgs = [m for m in STATE.get("messages", []) if m.get("pattern")==pattern and m["type"] in ("green","red")]
    if not msgs: return 0.0, 0
    msgs = msgs[-window:]
    g = sum(1 for m in msgs if m["type"]=="green")
    return (g/len(msgs), len(msgs))

def last_red_streak(pattern: str, max_check=5) -> int:
    msgs = [m for m in STATE.get("messages", []) if m.get("pattern")==pattern and m["type"] in ("green","red")]
    streak = 0
    for m in reversed(msgs[-max_check:]):
        if m["type"]=="red": streak += 1
        else: break
    return streak

def ban_pattern(pattern: str, reason: str):
    until = now_local() + timedelta(hours=BAN_FOR_HOURS)
    STATE["pattern_ban"][pattern] = {"until": until.isoformat(), "reason": reason}
    save_state()

def is_banned(pattern: str) -> bool:
    b = STATE["pattern_ban"].get(pattern)
    return bool(b) and datetime.fromisoformat(b["until"]) > now_local()

def cleanup_expired_bans():
    rm = [p for p,b in STATE["pattern_ban"].items() if datetime.fromisoformat(b["until"]) <= now_local()]
    for p in rm: STATE["pattern_ban"].pop(p, None)

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
    return k>0 and len(r)>=k and all(x=="R" for x in r[-k:])

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

# ========= TELEGRAM =========
async def tg_send(chat_id: int, text: str, disable_preview=True):
    async with httpx.AsyncClient(timeout=20) as cli:
        await cli.post(f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": disable_preview})

# ========= G0 HARDCORE =========
def passes_strict_g0(pattern: str) -> tuple[bool, str, float]:
    wr_glob, n_glob = rolling_wr(pattern)
    if n_glob < MIN_SAMPLES_BEFORE_FILTER:
        return False, f"amostra {n_glob}<{MIN_SAMPLES_BEFORE_FILTER}", wr_glob
    if wr_glob < MIN_G0:
        return False, f"WR {wr_glob:.2%}<{MIN_G0:.0%}", wr_glob
    lb = wilson_lower_bound(int(wr_glob*n_glob), n_glob, conf=WILSON_CONF)
    if lb < WILSON_MIN:
        return False, f"Wilson {lb:.2%}<{WILSON_MIN:.0%}", wr_glob
    wr_rec, n_rec = recent_wr(pattern, RECENT_WINDOW)
    if n_rec >= max(5, RECENT_WINDOW//3) and wr_rec < RECENT_MIN_WR:
        return False, f"WR recente {wr_rec:.2%}<{RECENT_MIN_WR:.0%} ({n_rec})", wr_glob
    if last_red_streak(pattern) >= BAN_AFTER_RED_STREAK:
        return False, "red recente", wr_glob
    if is_banned(pattern):
        return False, "padrão banido", wr_glob
    return True, "OK", wr_glob

# ========= PIPELINE =========
async def process_signal(text: str):
    daily_reset_if_needed()

    # fechamento automático: se chegar novo sinal e há operação aberta → fecha anterior como LOSS
    if STATE.get("open_signal"):
        await force_close_as_loss(reason="novo sinal com operação aberta")

    # Gate textual G0
    if not g0_gate_text(text):
        log.info("DESCARTADO (não é G0): %s", text[:80]); return

    pattern = extract_pattern(text)
    if not pattern:
        log.info("Sem padrão reconhecido"); return

    # Estatística hardcore
    ok, motivo, wr = passes_strict_g0(pattern)
    if not ok:
        log.info("REPROVADO G0: %s | %s", pattern, motivo); return

    # Risco / janelas / cap
    if not DISABLE_RISK:
        if STATE["daily_losses"] >= DAILY_STOP_LOSS:
            log.info("STOP diário"); return
        if cooldown_active():
            log.info("COOLDOWN ativo"); return
        if streak_guard_triggered():
            start_cooldown(); log.info("STREAK GUARD"); return
        if not hourly_cap_ok():
            log.info("CAP por hora"); return
    if not is_top_hour_now():
        log.info("FORA janela de ouro"); return

    # Aprovação final
    STATE["open_signal"] = {"ts": now_local().isoformat(), "text": text, "pattern": pattern}
    save_state()
    await tg_send(TARGET_CHAT_ID, f"🚀 <b>ENTRADA ABERTA</b>\n✅ G0 {wr*100:.1f}%\n{text}")
    register_hourly_entry()

async def process_result(text: str):
    is_green = bool(GREEN_RE.search(text))
    is_red   = bool(RED_RE.search(text))
    if not (is_green or is_red):
        return

    patt_hint = extract_pattern(text)

    # rolling + risco/global
    res = "G" if is_green else "R"
    if patt_hint: rolling_append(patt_hint, res)

    STATE["recent_g0"].append(res)
    STATE["recent_g0"] = STATE["recent_g0"][-10:]
    if res == "R": STATE["daily_losses"] = STATE.get("daily_losses", 0) + 1

    # contagem real
    if res == "G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] = STATE.get("streak_green", 0) + 1
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0

    # ban se  red streak do padrão
    if patt_hint and last_red_streak(patt_hint) >= BAN_AFTER_RED_STREAK:
        ban_pattern(patt_hint, f"{BAN_AFTER_RED_STREAK} RED seguidos")

    # fecha operação se havia aberta
    await close_and_summarize()

    # loga
    await aux_log({"ts": now_local().isoformat(), "type": "result_green" if is_green else "result_red",
                   "pattern": patt_hint, "text": text})
    save_state()

async def force_close_as_loss(reason: str):
    # usado quando chega novo sinal e havia operação aberta (fonte G2 etc.)
    STATE["totals"]["reds"] += 1
    STATE["streak_green"] = 0
    STATE["daily_losses"] = STATE.get("daily_losses", 0) + 1
    await close_and_summarize(extra=f"(fechado como LOSS: {reason})")

async def close_and_summarize(extra: str | None = None):
    if STATE.get("open_signal"):
        g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]
        total = g + r
        wr_day = (g/total*100.0) if total else 0.0
        resumo = f"✅ {g} ⛔️ {r} 🎯 Acertamos {wr_day:.2f}%\n🥇 ESTAMOS A {STATE['streak_green']} GREENS SEGUIDOS ⏳"
        if extra: resumo += f"\n<i>{extra}</i>"
        await tg_send(TARGET_CHAT_ID, resumo)
        STATE["open_signal"] = None
        save_state()

# ========= AUXILIARES =========
async def aux_log(entry: dict):
    STATE["messages"].append(entry)
    if len(STATE["messages"]) > 5000:
        STATE["messages"] = STATE["messages"][-4000:]
    save_state()

def build_report():
    g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]
    total = g + r
    wr_day = (g/total*100.0) if total else 0.0
    lines = [
        f"<b>📊 Relatório Diário ({today_str()})</b>",
        f"Dia: <b>{g}G / {r}R</b>  (WR: {wr_day:.1f}%)",
        f"Stop-loss: {DAILY_STOP_LOSS}  •  Perdas hoje: {STATE.get('daily_losses',0)}",
        "", "<b>Top horas (aprendidas):</b>",
    ]
    stats = STATE.get("hour_stats", {})
    rows = []
    for h,s in stats.items():
        if s["t"] >= TOP_HOURS_MIN_SAMPLES:
            rows.append((h, s["g"]/s["t"], s["t"]))
    rows.sort(key=lambda x: x[1], reverse=True)
    lines += [f"• {h}h → {wr*100:.1f}% ({n})" for h,wr,n in rows[:TOP_HOURS_COUNT]] or ["• Ainda aprendendo…"]
    return "\n".join(lines)

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

# ========= ROTAS =========
@app.get("/")
async def root():
    return {"ok": True, "service": "g0-hardcore (gate+wilson+recency)"}

@app.post("/webhook")
@app.post("/webhook/{secret}")
async def webhook(req: Request, secret: str | None = None):
    update = await req.json()
    if LOG_RAW: log.info("RAW UPDATE: %s", update)

    # message ou channel_post; lê text OU caption
    msg = update.get("channel_post") or update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or msg.get("caption") or ""
    msg_id = msg.get("message_id")

    # anti-duplicado
    if msg_id is not None:
        seen = STATE.get("seen_msg_ids", [])
        if msg_id in seen: return {"ok": True}
        STATE["seen_msg_ids"] = (seen + [msg_id])[-200:]
        save_state()

    if chat_id != SOURCE_CHAT_ID or not text:
        return {"ok": True}

    if FLOW_THROUGH:
        await tg_send(TARGET_CHAT_ID, text)
        return {"ok": True}

    # 1º: resultado
    if GREEN_RE.search(text) or RED_RE.search(text):
        await process_result(text)
        return {"ok": True}

    # 2º: potencial sinal
    low = text.lower()
    if (("entrada confirmada" in low) or re.search(r"\b(banker|player|empate|bac\s*bo|dados)\b", low)):
        await process_signal(text)
        return {"ok": True}

    return {"ok": True}