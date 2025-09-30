# app.py — pipeline G0 com fechamento de LOSS, auto-loss e +especialistas
import os, json, asyncio, re, pytz
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
import httpx
import logging
from collections import deque

# ============= ENV =============
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])   # origem
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])   # destino
TZ_NAME = os.getenv("TZ", "UTC")
LOCAL_TZ = pytz.timezone(TZ_NAME)

# Estratégia / limites (G0 + risco)
MIN_G0 = float(os.getenv("MIN_G0", "0.80"))
MIN_SAMPLES_BEFORE_FILTER = int(os.getenv("MIN_SAMPLES_BEFORE_FILTER", "20"))
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
FLOW_THROUGH = os.getenv("FLOW_THROUGH", "0") == "1"       # espelha tudo (teste)
LOG_RAW = os.getenv("LOG_RAW", "1") == "1"
DISABLE_WINDOWS = os.getenv("DISABLE_WINDOWS", "0") == "1"
DISABLE_RISK = os.getenv("DISABLE_RISK", "0") == "1"

# Novos toggles/parametrizações
AUTO_LOSS_MINUTES = int(os.getenv("AUTO_LOSS_MINUTES", "8"))     # timeout p/ loss
DUP_WINDOW_SEC    = int(os.getenv("DUP_WINDOW_SEC", "25"))       # janela anti-dup
ONLY_FANTAN       = os.getenv("ONLY_FANTAN", "1") == "1"         # filtra Bacará/Bac Bo
WL = [w.strip().lower() for w in os.getenv("WHITELIST_PATTERNS", "").split(",") if w.strip()]

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ============= APP/LOG =============
app = FastAPI()
log = logging.getLogger("uvicorn.error")

# ============= REGRAS DE TEXTO =============
# Padrões Fantan/Bac Bo (mantém termos úteis pro seu canal)
PATTERN_RE = re.compile(r"(banker|player|empate|bac\s*bo|fantan|dados|entrada\s*confirmada|odd|even)", re.I)
# Ruídos
NOISE_RE = re.compile(r"(bot\s*online|aposta\s*encerrada|analisando)", re.I)
# Resultado
GREEN_RE = re.compile(r"(green|win|✅)", re.I)
RED_RE   = re.compile(r"(red|lose|perd|loss|derrota|❌)", re.I)
# Menções que NÃO são resultado (ex.: “estamos no gale 1/2”)
GALE_PROGRESS_RE = re.compile(r"estamos\s+no\s+\d+º?\s*gale", re.I)

# Anti-dup (cache simples por texto)
_recent_msgs = {}  # text -> last_ts

def now_local(): return datetime.now(LOCAL_TZ)
def today_str(): return now_local().strftime("%Y-%m-%d")
def hour_str(dt=None): return (dt or now_local()).strftime("%H")
def hour_key(dt=None): return (dt or now_local()).strftime("%Y-%m-%d %H")

def extract_pattern(text: str) -> str|None:
    f = PATTERN_RE.findall(text or "")
    if not f: return None
    return "+".join(sorted([w.strip().lower() for w in f]))

def looks_like_fantan(text: str) -> bool:
    if not ONLY_FANTAN:
        return True
    low = (text or "").lower()
    # aceita termos que aparecem nos seus sinais de Fantan; rejeita Bacará explícito
    if "baccarat" in low:
        return False
    return ("fantan" in low) or ("entrada confirmada" in low) or ("odd" in low) or ("even" in low)

# ============= STATE =============
STATE = {
    "messages": [],           # [{ts,type,text,hour,pattern}]
    "last_reset_date": None,

    # rolling por padrão: pattern -> deque de "G"/"R"
    "pattern_roll": {},
    # métricas por hora
    "hour_stats": {},         # "00".."23" -> {"g":int,"t":int}
    # bans temporários
    "pattern_ban": {},        # pattern -> {"until": ISO, "reason": str}

    # risco / fluxo
    "daily_losses": 0,
    "hourly_entries": {},
    "cooldown_until": None,
    "recent_g0": [],

    # operação única + contagem real
    "open_signal": None,               # {"ts", "text", "pattern"}
    "totals": {"greens": 0, "reds": 0},
    "streak_green": 0,
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

# ============= LOGGING (aux) =============
async def aux_log_history(entry: dict):
    STATE["messages"].append(entry)
    if len(STATE["messages"]) > 5000:
        STATE["messages"] = STATE["messages"][-4000:]
    save_state()

# ============= MÉTRICAS/ROLLING =============
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
    for p in [p for p,b in STATE["pattern_ban"].items()
              if datetime.fromisoformat(b["until"]) <= now_local()]:
        STATE["pattern_ban"].pop(p, None)

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

# ============= RISCO / FLUXO =============
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

# ============= TELEGRAM =============
async def tg_send(chat_id: int, text: str, disable_preview=True):
    async with httpx.AsyncClient(timeout=20) as cli:
        await cli.post(f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": disable_preview})

# ============= G0 (gating) =============
def g0_allows(pattern: str) -> tuple[bool, float, int]:
    dq = STATE["pattern_roll"].get(pattern, deque())
    samples = len(dq)
    if samples < MIN_SAMPLES_BEFORE_FILTER:
        return True, 1.0, samples
    wr = rolling_wr(pattern)
    return (wr >= MIN_G0), wr, samples

# ============= FECHAMENTO (inclui auto-loss) =============
async def close_open_trade(as_green: bool|None):
    """Fecha operação aberta (se houver), atualiza contagem e publica resumo."""
    if as_green is None:
        return
    res = "G" if as_green else "R"

    # risco/contagem
    if res == "R":
        STATE["daily_losses"] = STATE.get("daily_losses", 0) + 1
        STATE["totals"]["reds"]  += 1
        STATE["streak_green"] = 0
    else:
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] = STATE.get("streak_green", 0) + 1

    # resumo
    g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]
    total = max(1, g + r)
    wr_day = (g/total*100.0)

    resumo = f"✅ {g} ⛔️ {r} 🎯 Acertamos {wr_day:.2f}%\n🥇 ESTAMOS A {STATE['streak_green']} GREENS SEGUIDOS ⏳"
    await tg_send(TARGET_CHAT_ID, resumo)

    STATE["open_signal"] = None
    save_state()

def auto_loss_due():
    """Se operação aberta estourou o tempo, marca LOSS automático."""
    op = STATE.get("open_signal")
    if not op: return False
    try:
        opened = datetime.fromisoformat(op["ts"])
    except Exception:
        return False
    return (now_local() - opened) >= timedelta(minutes=AUTO_LOSS_MINUTES)

# ============= PROCESSAMENTO ============
async def process_signal(text: str):
    daily_reset_if_needed()

    # Anti-duplicados
    ts_now = now_local().timestamp()
    last = _recent_msgs.get(text)
    _recent_msgs[text] = ts_now
    if last and (ts_now - last) < DUP_WINDOW_SEC:
        return

    # Se houver trade travado por tempo, fecha como LOSS automático
    if STATE.get("open_signal") and auto_loss_due():
        await close_open_trade(as_green=False)

    # 1 por vez
    if STATE.get("open_signal"):
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"ignored_open","text":text}))
        return

    low = (text or "").lower()
    if NOISE_RE.search(low) or GALE_PROGRESS_RE.search(low):
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"noise","text":text}))
        return

    # filtro de jogo
    if not looks_like_fantan(text):
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"other_game","text":text}))
        return

    # gatilho
    pattern = extract_pattern(text)
    gatilho = ("entrada confirmada" in low) or re.search(r"\b(banker|player|empate|bac\s*bo|dados)\b", low)
    if not gatilho:
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"no_trigger","text":text}))
        return

    if not pattern and "entrada confirmada" in low:
        pattern = "fantan"

    # whitelist opcional
    if WL:
        pcheck = (pattern or "").lower()
        if pcheck not in WL:
            asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"outside_whitelist","pattern":pattern,"text":text}))
            return

    # bans
    if is_banned(pattern):
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"banned_pattern","pattern":pattern,"text":text}))
        return

    # G0
    allowed, wr, samples = g0_allows(pattern)
    if not allowed:
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"rejected_g0","pattern":pattern,"wr":wr,"n":samples,"text":text}))
        return

    # Risco
    if not DISABLE_RISK:
        if STATE["daily_losses"] >= DAILY_STOP_LOSS:
            asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"stop_daily","text":text}))
            return
        if cooldown_active():
            asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"cooldown","text":text}))
            return
        if streak_guard_triggered():
            start_cooldown()
            asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"streak_guard","text":text}))
            return
        if not hourly_cap_ok():
            asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"hour_cap","text":text}))
            return

    # Janelas de ouro
    if not is_top_hour_now():
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"outside_window","text":text}))
        return

    # Aprovado
    STATE["open_signal"] = {"ts": now_local().isoformat(), "text": text, "pattern": pattern}
    save_state()
    await tg_send(TARGET_CHAT_ID, f"🚀 <b>ENTRADA ABERTA</b>\n✅ G0 {wr*100:.1f}% ({samples} am.)\n{text}")
    register_hourly_entry()

async def process_result(text: str):
    # Anti-dup
    ts_now = now_local().timestamp()
    last = _recent_msgs.get(text)
    _recent_msgs[text] = ts_now
    if last and (ts_now - last) < DUP_WINDOW_SEC:
        return

    low = (text or "").lower()
    if GALE_PROGRESS_RE.search(low):
        # progresso de gale não fecha operação
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"gale_progress","text":text}))
        return

    patt_hint = extract_pattern(text)
    is_green = bool(GREEN_RE.search(text))
    is_red   = bool(RED_RE.search(text))
    if not (is_green or is_red):
        return

    # Atualiza rolling se conseguir extrair pattern
    if patt_hint:
        rolling_append(patt_hint, "G" if is_green else "R")

        # Ban daquele padrão se bateu R consecutivos
        if BAN_AFTER_CONSECUTIVE_R > 0:
            tail = pattern_recent_tail(patt_hint, BAN_AFTER_CONSECUTIVE_R)
            if tail and tail.endswith("R"*BAN_AFTER_CONSECUTIVE_R):
                ban_pattern(patt_hint, f"{BAN_AFTER_CONSECUTIVE_R} REDS seguidos")

    # Alimenta streak_guard e fecha operação (mesmo sem pattern!)
    STATE["recent_g0"].append("G" if is_green else "R")
    STATE["recent_g0"] = STATE["recent_g0"][-10:]

    # Se tem operação aberta → fecha com o resultado recebido
    if STATE.get("open_signal"):
        await close_open_trade(as_green=is_green)
    else:
        # Sem operação aberta, ainda assim contabiliza o dia (opcional, desligável se quiser)
        await close_open_trade(as_green=is_green)

    asyncio.create_task(aux_log_history({
        "ts": now_local().isoformat(),
        "type": "result_green" if is_green else "result_red",
        "pattern": patt_hint, "text": text
    }))
    save_state()

# ============= RELATÓRIO =============
def build_report():
    g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]
    total = g + r
    wr_day = (g/total*100.0) if total else 0.0

    rows = []
    for p, dq in STATE.get("pattern_roll", {}).items():
        n = len(dq)
        if n >= 10:
            wr = sum(1 for x in dq if x=="G")/n
            rows.append((p, wr, n))
    rows.sort(key=lambda x: x[1], reverse=True)
    tops = rows[:5]

    # banidos ativos
    cleanup_expired_bans()
    bans = []
    for p, b in STATE.get("pattern_ban", {}).items():
        until = datetime.fromisoformat(b["until"]).strftime("%d/%m %H:%M")
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
    return {"ok": True, "service": "g0-pipeline (loss/auto-loss + especialistas)"}

# Processa apenas channel_post (evita duplicados do Telegram)
@app.post("/webhook")
@app.post("/webhook/{secret}")
async def webhook(req: Request, secret: str|None=None):
    update = await req.json()
    if LOG_RAW: log.info("RAW UPDATE: %s", update)

    msg = update.get("channel_post") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text") or ""

    if not text or chat_id != SOURCE_CHAT_ID:
        return {"ok": True}

    # Modo espelho (debug)
    if FLOW_THROUGH:
        await tg_send(TARGET_CHAT_ID, text)
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"mirror","text": text}))
        return {"ok": True}

    low = text.lower()

    # resultados primeiro (GREEN/RED)
    if GREEN_RE.search(text) or RED_RE.search(text):
        await process_result(text)
        return {"ok": True}

    # progresso de gale não fecha nem abre
    if GALE_PROGRESS_RE.search(low):
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"gale_progress","text":text}))
        return {"ok": True}

    # potenciais sinais
    if (("entrada confirmada" in low) or
        re.search(r"\b(banker|player|empate|bac\s*bo|dados)\b", low)):
        await process_signal(text)
        return {"ok": True}

    # ruídos/outs
    if NOISE_RE.search(low):
        asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"noise","text":text}))
        return {"ok": True}

    asyncio.create_task(aux_log_history({"ts": now_local().isoformat(),"type":"other","text":text}))
    return {"ok": True}