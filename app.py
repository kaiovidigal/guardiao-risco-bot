# app.py
import os, json, asyncio, re, pytz
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
import httpx
import logging

# ========= ENV =========
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]                # ex: 12345:ABC...
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])       # -1003156785631
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])       # -1002796105884
TZ_NAME = os.getenv("TZ", "UTC")

# Filtros (podem ser relaxados por ENV)
MIN_G0 = float(os.getenv("MIN_G0", "0.80"))
DAILY_STOP_LOSS = int(os.getenv("DAILY_STOP_LOSS", "3"))
STREAK_GUARD_LOSSES = int(os.getenv("STREAK_GUARD_LOSSES", "2"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))
HOURLY_CAP = int(os.getenv("HOURLY_CAP", "6"))
TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.85"))
TOP_HOURS_COUNT = int(os.getenv("TOP_HOURS_COUNT", "6"))
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "25"))
MIN_SAMPLES_BEFORE_FILTER = int(os.getenv("MIN_SAMPLES_BEFORE_FILTER", "20"))

# Toggles
FLOW_THROUGH  = os.getenv("FLOW_THROUGH",  "0") == "1"   # espelha tudo
DISABLE_WINDOWS = os.getenv("DISABLE_WINDOWS", "0") == "1"
DISABLE_RISK    = os.getenv("DISABLE_RISK",    "0") == "1"
LOG_RAW       = os.getenv("LOG_RAW",       "1") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
LOCAL_TZ = pytz.timezone(TZ_NAME)

logger = logging.getLogger("uvicorn.error")
app = FastAPI()

# ========= STATE =========
STATE = {
    "messages": [],           # [{ts,type,text,hour,pattern}]
    "pattern_stats": {},      # pattern -> {"g0_green": int, "g0_total": int}
    "hour_stats": {},         # "00".."23" -> {"g0_green": int, "g0_total": int}
    "last_reset_date": None,  # "YYYY-MM-DD"
    "daily_losses": 0,
    "hourly_entries": {},     # "YYYY-MM-DD HH" -> int
    "cooldown_until": None,   # ISO
    "recent_g0": [],          # últimos resultados: "G"/"R"
}

def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def save_state():
    _ensure_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)

def load_state():
    global STATE
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            STATE = json.load(f)
    except Exception:
        save_state()
load_state()

# ========= UTILS =========
def now_local(): return datetime.now(LOCAL_TZ)
def today_str(): return now_local().strftime("%Y-%m-%d")
def hour_key(dt=None):
    if dt is None: dt = now_local()
    return dt.strftime("%Y-%m-%d %H")

async def tg_send(chat_id: int, text: str, disable_preview=True):
    async with httpx.AsyncClient(timeout=20) as cli:
        await cli.post(f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": disable_preview})

# ========= PADRÕES (amplo p/ Bac Bo) =========
PATTERN_RE = re.compile(
    r"(banker|player|empate|bac\s*bo|dados|banker\s*\+\s*empate|player\s*\+\s*empate|g0|g1)",
    re.I,
)

def extract_pattern(text: str) -> str | None:
    found = PATTERN_RE.findall(text or "")
    if not found: return None
    return "+".join(sorted([w.strip().lower() for w in found]))

# ========= ESTATÍSTICA =========
def add_message(msg_type: str, text: str, pattern: str | None):
    dt = now_local()
    STATE["messages"].append({
        "ts": dt.isoformat(), "type": msg_type, "text": text,
        "hour": dt.strftime("%H"), "pattern": pattern})
    save_state()

def register_outcome_g0(result: str, pattern_hint: str | None):
    if result not in ("G","R"): return
    pattern = pattern_hint
    if not pattern:
        for m in reversed(STATE["messages"]):
            if m["type"]=="signal" and m.get("pattern"):
                pattern = m["pattern"]; break
    if not pattern: return

    ps = STATE["pattern_stats"].setdefault(pattern, {"g0_green":0,"g0_total":0})
    ps["g0_total"] += 1
    if result=="G": ps["g0_green"] += 1

    h = now_local().strftime("%H")
    hs = STATE["hour_stats"].setdefault(h, {"g0_green":0,"g0_total":0})
    hs["g0_total"] += 1
    if result=="G": hs["g0_green"] += 1

    STATE["recent_g0"].append(result)
    STATE["recent_g0"] = STATE["recent_g0"][-10:]
    if result=="R": STATE["daily_losses"] = STATE.get("daily_losses",0) + 1
    save_state()

def g0_winrate(pattern: str) -> float:
    ps = STATE["pattern_stats"].get(pattern)
    if not ps or ps["g0_total"]==0: return 0.0
    return ps["g0_green"]/ps["g0_total"]

def is_top_hour_now() -> bool:
    stats = STATE.get("hour_stats", {})
    items = [(h,s) for h,s in stats.items() if s["g0_total"]>=TOP_HOURS_MIN_SAMPLES]
    if not items: return True
    ranked = sorted(items, key=lambda kv: (kv[1]["g0_green"]/kv[1]["g0_total"]), reverse=True)
    top = []
    for h,s in ranked:
        wr = s["g0_green"]/s["g0_total"]
        if wr >= TOP_HOURS_MIN_WINRATE: top.append(h)
        if len(top) >= TOP_HOURS_COUNT: break
    return now_local().strftime("%H") in top if top else True

def hourly_cap_ok() -> bool:
    return STATE["hourly_entries"].get(hour_key(),0) < HOURLY_CAP

def register_hourly_entry():
    k = hour_key()
    STATE["hourly_entries"][k] = STATE["hourly_entries"].get(k,0) + 1
    save_state()

def cooldown_active() -> bool:
    cu = STATE.get("cooldown_until")
    return bool(cu) and datetime.fromisoformat(cu) > now_local()

def start_cooldown():
    STATE["cooldown_until"] = (now_local()+timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
    save_state()

def streak_guard_triggered() -> bool:
    k = STREAK_GUARD_LOSSES
    r = STATE.get("recent_g0", [])
    return k>0 and len(r)>=k and all(x=="R" for x in r[-k:])

def daily_reset_if_needed():
    today = today_str()
    if STATE.get("last_reset_date") != today:
        STATE["last_reset_date"] = today
        STATE["daily_losses"] = 0
        STATE["hourly_entries"] = {}
        STATE["cooldown_until"] = None
        save_state()

# ========= PIPELINE =========
async def process_signal(text: str):
    daily_reset_if_needed()
    pattern = extract_pattern(text)
    add_message("signal", text, pattern)
    if not pattern:
        logger.info("DESCARTADO: sem padrão | %s", text); return

    ps = STATE["pattern_stats"].get(pattern, {"g0_green":0,"g0_total":0})
    wr = 1.0 if ps["g0_total"] < MIN_SAMPLES_BEFORE_FILTER else g0_winrate(pattern)
    if wr < MIN_G0:
        logger.info("DESCARTADO: wr=%.2f < MIN_G0=%.2f | %s total=%s", wr, MIN_G0, pattern, ps["g0_total"]); return
    if not DISABLE_WINDOWS and not is_top_hour_now():
        logger.info("DESCARTADO: fora da Janela de Ouro"); return
    if not DISABLE_RISK:
        if STATE["daily_losses"] >= DAILY_STOP_LOSS: logger.info("DESCARTADO: stop-loss diário"); return
        if cooldown_active(): logger.info("DESCARTADO: cooldown ativo"); return
        if streak_guard_triggered(): start_cooldown(); logger.info("DESCARTADO: streak guard"); return
        if not hourly_cap_ok(): logger.info("DESCARTADO: limite por hora"); return

    await tg_send(TARGET_CHAT_ID, f"✅ <b>G0 {wr*100:.1f}%</b>\n{text}")
    register_hourly_entry()

async def process_result(text: str):
    patt_hint = extract_pattern(text)
    t = (text or "").lower()
    if "green" in t or "win" in t or "✅" in t:
        add_message("green", text, patt_hint); register_outcome_g0("G", patt_hint)
    elif "red" in t or "lose" in t or "perd" in t or "❌" in t:
        add_message("red", text, patt_hint); register_outcome_g0("R", patt_hint)

# ========= RELATÓRIO DIÁRIO =========
def build_daily_report():
    today = today_str()
    msgs = [m for m in STATE["messages"] if m["ts"].startswith(today)]
    greens = sum(1 for m in msgs if m["type"]=="green")
    reds   = sum(1 for m in msgs if m["type"]=="red")
    total  = greens+reds
    wr_day = (greens/total) if total else 0.0

    tops = []
    for p,s in STATE["pattern_stats"].items():
        if s["g0_total"]>=5:
            tops.append((p, s["g0_green"]/s["g0_total"], s["g0_total"]))
    tops.sort(key=lambda x:x[1], reverse=True); tops = tops[:5]

    hs = STATE.get("hour_stats", {})
    hours_rank = []
    for h,s in hs.items():
        if s["g0_total"]>=TOP_HOURS_MIN_SAMPLES:
            wr = s["g0_green"]/s["g0_total"]
            if wr>=TOP_HOURS_MIN_WINRATE: hours_rank.append((h,wr,s["g0_total"]))
    hours_rank.sort(key=lambda x:x[1], reverse=True); hours_rank = hours_rank[:TOP_HOURS_COUNT]

    lines = [
        "<b>📊 Relatório Diário (G0)</b>",
        f"Data: {today}",
        f"Resultado: <b>{greens}G / {reds}R</b>  (WR: {wr_day*100:.1f}% | {total} jogos)",
        f"Stop-loss: {DAILY_STOP_LOSS}  •  Perdas hoje: {STATE.get('daily_losses',0)}",
        "", "<b>Top padrões:</b>",
    ]
    lines += [f"• {p} → {wr*100:.1f}% ({n})" for p,wr,n in tops] or ["• Sem dados."]
    lines += ["", "<b>Janelas de Ouro:</b>"]
    lines += [f"• {h}h → {wr*100:.1f}% ({n})" for h,wr,n in hours_rank] or ["• Ainda aprendendo…"]
    return "\n".join(lines)

async def daily_report_loop():
    await asyncio.sleep(5)
    while True:
        try:
            now = now_local()
            if now.hour == 0 and now.minute == 0:
                daily_reset_if_needed()
                await tg_send(TARGET_CHAT_ID, build_daily_report())
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
    return {"ok": True, "service": "bacbo-g0-bot"}

# aceita /webhook e /webhook/
@app.post("/webhook")
@app.post("/webhook/")
async def webhook_base(req: Request):
    return await process_update(req)

# aceita /webhook/<segredo> e /webhook/<segredo>/
@app.post("/webhook/{secret}")
@app.post("/webhook/{secret}/")
async def webhook_secret(secret: str, req: Request):
    return await process_update(req)

async def process_update(req: Request):
    update = await req.json()
    if LOG_RAW: logger.info("RAW UPDATE: %s", update)

    msg  = update.get("message") or update.get("channel_post") or {}
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    text = msg.get("text", "") or ""

    # 🔓 Fluxo livre: espelha tudo do SOURCE
    if FLOW_THROUGH and (chat_id == SOURCE_CHAT_ID) and text:
        await tg_send(TARGET_CHAT_ID, text)
        return {"ok": True}

    # Lógica padrão
    if chat_id == SOURCE_CHAT_ID and text:
        low = text.lower()
        if any(k in low for k in ["banker","player","empate","bac bo","dados","g0","g1"]):
            await process_signal(text)
        if any(k in low for k in ["green","win","✅","red","lose","❌","perd"]):
            await process_result(text)
    return {"ok": True}

# cron manual
@app.get("/cron/daily_report")
async def cron_daily():
    daily_reset_if_needed()
    await tg_send(TARGET_CHAT_ID, build_daily_report())
    return {"ok": True, "sent": True}