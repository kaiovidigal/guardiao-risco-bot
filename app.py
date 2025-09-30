import os, json, asyncio, re, pytz
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
import httpx
import logging

# ===== ENV =====
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])    # origem
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])    # destino
TZ_NAME = os.getenv("TZ", "UTC")

MIN_G0 = float(os.getenv("MIN_G0", "0.80"))
MIN_SAMPLES_BEFORE_FILTER = int(os.getenv("MIN_SAMPLES_BEFORE_FILTER", "20"))
DAILY_STOP_LOSS = int(os.getenv("DAILY_STOP_LOSS", "3"))
STREAK_GUARD_LOSSES = int(os.getenv("STREAK_GUARD_LOSSES", "2"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))
HOURLY_CAP = int(os.getenv("HOURLY_CAP", "6"))
TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.85"))
TOP_HOURS_COUNT = int(os.getenv("TOP_HOURS_COUNT", "6"))
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "25"))

FLOW_THROUGH = os.getenv("FLOW_THROUGH", "0") == "1"   # espelha tudo (teste)
LOG_RAW      = os.getenv("LOG_RAW", "1") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
LOCAL_TZ = pytz.timezone(TZ_NAME)
log = logging.getLogger("uvicorn.error")

app = FastAPI()

# ===== STATE =====
STATE = {
    "messages": [],           # [{ts,type,text,hour,pattern}]
    "pattern_stats": {},      # pattern -> {"g0_green": int, "g0_total": int}
    "hour_stats": {},         # "00".."23" -> {"g0_green": int, "g0_total": int}
    "last_reset_date": None,
    "daily_losses": 0,
    "hourly_entries": {},
    "cooldown_until": None,
    "recent_g0": [],

    # operação única + contagem real
    "open_signal": None,                 # {"ts": ISO, "text": str, "pattern": str}
    "totals": {"greens": 0, "reds": 0},
    "streak_green": 0,
}

def _ensure_dir(p): 
    d = os.path.dirname(p)
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

# ===== UTILS =====
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

# ===== REGRAS DE TEXTO =====
PATTERN_RE = re.compile(r"(banker|player|empate|bac\s*bo|dados|entrada\s*confirmada|odd|even)", re.I)
NOISE_RE   = re.compile(r"(bot\s*online|estamos\s+no\s+\d+º?\s*gale|aposta\s*encerrada|analisando)", re.I)

def extract_pattern(text: str) -> str | None:
    f = PATTERN_RE.findall(text or "")
    if not f: return None
    return "+".join(sorted([w.strip().lower() for w in f]))

# ===== ESTATÍSTICA =====
def add_message(tp, text, pattern):
    dt = now_local()
    STATE["messages"].append({"ts": dt.isoformat(), "type": tp, "text": text,
                              "hour": dt.strftime("%H"), "pattern": pattern})
    save_state()

def register_outcome_g0(result, pattern_hint):
    if result not in ("G","R"): return
    pattern = pattern_hint
    if not pattern:
        for m in reversed(STATE["messages"]):
            if m["type"]=="signal" and m.get("pattern"): pattern = m["pattern"]; break
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

def g0_wr(pattern: str) -> float:
    ps = STATE["pattern_stats"].get(pattern)
    return (ps["g0_green"]/ps["g0_total"]) if ps and ps["g0_total"] else 0.0

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

def hourly_cap_ok(): return STATE["hourly_entries"].get(hour_key(),0) < HOURLY_CAP
def register_hourly_entry():
    k = hour_key(); STATE["hourly_entries"][k] = STATE["hourly_entries"].get(k,0) + 1; save_state()

def cooldown_active():
    cu = STATE.get("cooldown_until"); 
    return bool(cu) and datetime.fromisoformat(cu) > now_local()

def start_cooldown():
    STATE["cooldown_until"] = (now_local()+timedelta(minutes=COOLDOWN_MINUTES)).isoformat(); save_state()

def streak_guard_triggered():
    k = STREAK_GUARD_LOSSES; r = STATE.get("recent_g0", [])
    return k>0 and len(r)>=k and all(x=="R" for x in r[-k:])

def daily_reset_if_needed():
    if STATE.get("last_reset_date") != today_str():
        STATE["last_reset_date"] = today_str()
        STATE["daily_losses"] = 0
        STATE["hourly_entries"] = {}
        STATE["cooldown_until"] = None
        STATE["totals"] = {"greens": 0, "reds": 0}
        STATE["streak_green"] = 0
        STATE["open_signal"] = None
        save_state()

# ===== PIPELINE =====
async def process_signal(text: str):
    daily_reset_if_needed()

    # 1 por vez
    if STATE.get("open_signal"):
        log.info("Ignorado: operação aberta"); return

    low = (text or "").lower()
    if NOISE_RE.search(low): 
        log.info("Ruído: %s", text); return

    pattern = extract_pattern(text)
    gatilho = ("entrada confirmada" in low) or re.search(r"\b(banker|player|empate|bac\s*bo|dados)\b", low)
    if not gatilho:
        log.info("Sem gatilho de sinal"); return

    if not pattern and "entrada confirmada" in low:
        pattern = "fantan"

    ps = STATE["pattern_stats"].get(pattern, {"g0_green":0,"g0_total":0})
    wr = 1.0 if ps["g0_total"] < MIN_SAMPLES_BEFORE_FILTER else g0_wr(pattern)
    if wr < MIN_G0:
        log.info("Reprovado G0: wr=%.2f < %.2f / amostras=%d", wr, MIN_G0, ps["g0_total"]); return

    if not is_top_hour_now(): log.info("Fora da janela de ouro"); return
    if STATE["daily_losses"] >= DAILY_STOP_LOSS: log.info("Stop-loss diário"); return
    if cooldown_active(): log.info("Cooldown ativo"); return
    if streak_guard_triggered(): start_cooldown(); log.info("Streak guard"); return
    if not hourly_cap_ok(): log.info("Limite por hora"); return

    # abre operação
    STATE["open_signal"] = {"ts": now_local().isoformat(), "text": text, "pattern": pattern}
    save_state()
    await tg_send(TARGET_CHAT_ID, f"✅ <b>G0 {wr*100:.1f}%</b>\n{text}")
    register_hourly_entry()

async def process_result(text: str):
    low = (text or "").lower()
    patt_hint = extract_pattern(text)

    # palavras de GREEN / RED (ampla)
    is_green = ("green" in low or "win" in low or "✅" in text)
    is_red   = ("red" in low or "lose" in low or "perd" in low or "❌" in text or "loss" in low or "derrota" in low)

    if not (is_green or is_red):
        return

    add_message("green" if is_green else "red", text, patt_hint)
    register_outcome_g0("G" if is_green else "R", patt_hint)

    if is_green:
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] = STATE.get("streak_green", 0) + 1
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0
    save_state()

    # fecha se havia operação
    if STATE.get("open_signal"):
        g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]
        total = g + r
        wr = (g/total*100.0) if total else 0.0
        resumo = f"✅ {g} ⛔️ {r} 🎯 Acertamos {wr:.2f}%\n🥇 ESTAMOS A {STATE['streak_green']} GREENS SEGUIDOS ⏳"
        await tg_send(TARGET_CHAT_ID, resumo)
        STATE["open_signal"] = None
        save_state()

# ===== RELATÓRIO (opcional) =====
def build_daily_report():
    today = today_str()
    msgs = [m for m in STATE["messages"] if m["ts"].startswith(today)]
    greens = sum(1 for m in msgs if m["type"]=="green")
    reds   = sum(1 for m in msgs if m["type"]=="red")
    total  = greens+reds
    wr_day = (greens/total) if total else 0.0
    return (f"<b>📊 Relatório ({today})</b>\n"
            f"Dia: {greens}G/{reds}R  (WR {wr_day*100:.1f}%)\n"
            f"Stop-loss:{DAILY_STOP_LOSS}  Perdas hoje:{STATE.get('daily_losses',0)}")

async def daily_report_loop():
    await asyncio.sleep(5)
    while True:
        try:
            n = now_local()
            if n.hour==0 and n.minute==0:
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

# ===== ROTAS =====
@app.get("/")
async def root():
    return {"ok": True, "service": "g0-bot (single-trade+count)"}

# Aceita: /webhook e /webhook/<segredo>
@app.post("/webhook")
@app.post("/webhook/{secret}")
async def webhook(req: Request, secret: str | None = None):
    update = await req.json()
    if LOG_RAW: log.info("RAW UPDATE: %s", update)

    msg = update.get("channel_post") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text") or ""

    if FLOW_THROUGH and chat_id == SOURCE_CHAT_ID and text:
        await tg_send(TARGET_CHAT_ID, text); return {"ok": True}

    if chat_id == SOURCE_CHAT_ID and text:
        low = text.lower()
        if any(k in low for k in ["entrada confirmada","banker","player","empate","bac bo","dados"]):
            await process_signal(text)
        if any(k in low for k in ["green","win","✅","red","lose","❌","perd","loss","derrota"]):
            await process_result(text)

    return {"ok": True}