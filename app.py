# app.py — G0 com especialistas (sem travar) + dedupe + contagem real
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

# Estratégia / limites
MIN_G0 = float(os.getenv("MIN_G0", "0.80"))                 # corte base do G0
MIN_SAMPLES_BEFORE_FILTER = int(os.getenv("MIN_SAMPLES_BEFORE_FILTER", "20"))
ROLLING_MAX = int(os.getenv("ROLLING_MAX", "500"))
BAN_AFTER_CONSECUTIVE_R = int(os.getenv("BAN_AFTER_CONSECUTIVE_R", "2"))
BAN_FOR_HOURS = int(os.getenv("BAN_FOR_HOURS", "4"))

DAILY_STOP_LOSS = int(os.getenv("DAILY_STOP_LOSS", "3"))
STREAK_GUARD_LOSSES = int(os.getenv("STREAK_GUARD_LOSSES", "2"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))
HOURLY_CAP = int(os.getenv("HOURLY_CAP", "8"))

TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.85"))
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "25"))
TOP_HOURS_COUNT = int(os.getenv("TOP_HOURS_COUNT", "8"))

# Fluxo sem travar
MIN_GAP_SECS = int(os.getenv("MIN_GAP_SECS", "12"))  # intervalo mínimo entre publicações

# Score dos especialistas (0–1). Só bloqueamos se score_final < SCORE_MIN.
SCORE_MIN = float(os.getenv("SCORE_MIN", "0.55"))

# Pesos (soma ~1.0)
W_TEXT = float(os.getenv("W_TEXT", "0.15"))
W_SEQ  = float(os.getenv("W_SEQ", "0.25"))
W_NGRAM= float(os.getenv("W_NGRAM","0.25"))
W_ADAPT= float(os.getenv("W_ADAPT","0.35"))

# Toggles
FLOW_THROUGH   = os.getenv("FLOW_THROUGH", "0") == "1"      # espelha tudo
LOG_RAW        = os.getenv("LOG_RAW", "1") == "1"
DISABLE_RISK   = os.getenv("DISABLE_RISK", "0") == "1"
DISABLE_WIN    = os.getenv("DISABLE_WINDOWS", "0") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ============= APP/LOG =============
app = FastAPI()
log = logging.getLogger("uvicorn.error")

# ============= REGRAS DE TEXTO =============
PATTERN_RE = re.compile(r"(banker|player|empate|bac\s*bo|fantan|dados|entrada\s*confirmada|odd|even)", re.I)
NOISE_RE   = re.compile(r"(bot\s*online|estamos\s+no\s+\d+º?\s*gale|aposta\s*encerrada|analisando)", re.I)
GREEN_RE   = re.compile(r"(green|win|✅)", re.I)
RED_RE     = re.compile(r"(red|lose|perd|loss|derrota|❌)", re.I)
SEQ_LINE_RE= re.compile(r"sequ[eê]ncia\s*[:\-]\s*([^\n\r]+)", re.I)

def now_local(): return datetime.now(LOCAL_TZ)
def today_str(): return now_local().strftime("%Y-%m-%d")
def hour_str(dt=None): return (dt or now_local()).strftime("%H")
def hour_key(dt=None): return (dt or now_local()).strftime("%Y-%m-%d %H")

def extract_pattern(text: str) -> str|None:
    f = PATTERN_RE.findall(text or "")
    if not f: return None
    return "+".join(sorted([w.strip().lower() for w in f]))

def text_sig(s: str) -> str:
    return hashlib.md5((s or "").strip().lower().encode()).hexdigest()

# ============= STATE =============
STATE = {
    "messages": [],                 # histórico leve
    "last_reset_date": None,

    "pattern_roll": {},             # pattern -> deque de "G"/"R"
    "hour_stats": {},               # "HH" -> {"g":int,"t":int}
    "pattern_ban": {},              # pattern -> {"until": ISO, "reason": str}

    # risco / fluxo / resumo
    "daily_losses": 0,
    "hourly_entries": {},
    "cooldown_until": None,
    "recent_g0": [],                # últimos G/R
    "open_signal": None,            # {"ts","text","pattern"}
    "totals": {"greens": 0, "reds": 0},
    "streak_green": 0,

    # anti-spam / dedupe
    "last_publish_ts": None,
    "seen_update_ids": deque(maxlen=2000),
    "seen_text_sigs": deque(maxlen=500),
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

# ============= HISTÓRICO (assíncrono) =============
async def aux_log(entry: dict):
    STATE["messages"].append(entry)
    if len(STATE["messages"]) > 5000:
        STATE["messages"] = STATE["messages"][-4000:]
    save_state()

# ============= ROLLING/MÉTRICAS =============
def rolling_append(pattern: str, result: str):
    dq = STATE["pattern_roll"].setdefault(pattern, deque(maxlen=ROLLING_MAX))
    dq.append(result)
    # hour stats
    h = hour_str()
    hs = STATE["hour_stats"].setdefault(h, {"g": 0, "t": 0})
    hs["t"] += 1
    if result == "G": hs["g"] += 1

def rolling_wr(pattern: str) -> float:
    dq = STATE["pattern_roll"].get(pattern)
    if not dq: return 0.0
    return sum(1 for x in dq if x == "G")/len(dq)

def pattern_tail(pattern: str, k: int) -> str:
    dq = STATE["pattern_roll"].get(pattern) or []
    return "".join(list(dq)[-k:])

def ban_pattern(pattern: str, reason: str):
    until = now_local() + timedelta(hours=BAN_FOR_HOURS)
    STATE["pattern_ban"][pattern] = {"until": until.isoformat(), "reason": reason}
    save_state()

def is_banned(pattern: str) -> bool:
    b = STATE["pattern_ban"].get(pattern)
    return bool(b) and datetime.fromisoformat(b["until"]) > now_local()

def cleanup_bans():
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
        cleanup_bans()
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

def hourly_cap_ok():
    return STATE["hourly_entries"].get(hour_key(), 0) < HOURLY_CAP

def register_hourly_entry():
    k = hour_key()
    STATE["hourly_entries"][k] = STATE["hourly_entries"].get(k, 0) + 1
    save_state()

def is_top_hour_now():
    if DISABLE_WIN: return True
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

# ============= ESPECIALISTA FIXO (G0) =============
def g0_allows(pattern: str) -> tuple[bool, float, int]:
    dq = STATE["pattern_roll"].get(pattern, deque())
    n = len(dq)
    if n < MIN_SAMPLES_BEFORE_FILTER:
        return True, 1.0, n
    wr = rolling_wr(pattern)
    return (wr >= MIN_G0), wr, n

# ============= ESPECIALISTAS AUXILIARES (score) =============
def spec_text_quality(text: str) -> float:
    """Penaliza propaganda pura, links e textos muito curtos."""
    t = text.strip()
    if len(t) < 20: return 0.3
    penalty = 0.0
    if "http" in t.lower(): penalty += 0.25
    if t.count("\n") <= 1: penalty += 0.1
    return max(0.0, 1.0 - penalty)

def spec_sequence_heuristic(text: str) -> float:
    """Lê 'Sequência:'; se padrões curtos/limpos → favorece G0."""
    m = SEQ_LINE_RE.search(text or "")
    if not m: return 0.6  # neutro levemente positivo
    seq = re.sub(r"[^\dXx| ]", "", m.group(1))
    # heurística: poucas casas e pouca alternância favorecem G0
    tokens = [s for s in re.split(r"[| ]+", seq) if s]
    if not tokens: return 0.6
    unique = len(set(tokens))
    if len(tokens) <= 5 and unique <= 3: return 0.9
    if len(tokens) <= 7 and unique <= 4: return 0.75
    return 0.55

def spec_ngram_score(text: str) -> float:
    """Keywords fortes para G0 (placeholder leve)."""
    t = text.lower()
    score = 0.5
    if "entrada confirmada" in t: score += 0.2
    if re.search(r"\b(player\s*\+\s*empate|banker\s*\+\s*empate)\b", t): score += 0.15
    if re.search(r"\b(odd|even)\b", t): score += 0.05
    return min(1.0, score)

def spec_adaptive_threshold() -> float:
    """Se houve R recentes, sobe exigência → score cai."""
    r = STATE.get("recent_g0", [])
    if not r: return 1.0
    last3 = r[-3:]
    reds = last3.count("R")
    if reds == 0: return 1.0
    if reds == 1: return 0.9
    if reds == 2: return 0.75
    return 0.6

def combined_score(text: str) -> float:
    return (
        W_TEXT  * spec_text_quality(text) +
        W_SEQ   * spec_sequence_heuristic(text) +
        W_NGRAM * spec_ngram_score(text) +
        W_ADAPT * spec_adaptive_threshold()
    )

# ========= FORMATAÇÃO (cores Player/Banker/Empate) =========
def colorize_targets(text: str) -> str:
    t = text
    t = re.sub(r"\bplayer\b", "🔵 <b>Player</b>", t, flags=re.I)
    t = re.sub(r"\bbanker\b", "🔴 <b>Banker</b>", t, flags=re.I)
    t = re.sub(r"\bempate\b", "🟡 <b>Empate</b>", t, flags=re.I)
    return t

# ============= PROCESSAMENTO ============
async def process_signal(text: str):
    daily_reset_if_needed()

    low = (text or "").lower()
    # ruído
    if NOISE_RE.search(low):
        log.info("DESCARTADO: ruído")
        asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"noise","text":text}))
        return

    # gatilho
    pattern = extract_pattern(text)
    gatilho = ("entrada confirmada" in low) or re.search(r"\b(banker|player|empate|bac\s*bo|fantan|dados)\b", low)
    if not gatilho:
        log.info("DESCARTADO: sem gatilho")
        asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"no_trigger","text":text}))
        return
    if not pattern and "entrada confirmada" in low:
        pattern = "fantan"

    # bans
    if pattern and is_banned(pattern):
        log.info("DESCARTADO: padrão banido")
        asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"banned","pattern":pattern,"text":text}))
        return

    # G0 estatístico
    allowed, wr, samples = g0_allows(pattern or "generic")
    if not allowed:
        log.info("REPROVADO G0: wr=%.2f n=%d (min=%.2f)", wr, samples, MIN_G0)
        asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"rejected_g0","wr":wr,"n":samples,"text":text}))
        return

    # Risco real (stop/cooldown). O resto é aviso.
    if not DISABLE_RISK:
        if STATE["daily_losses"] >= DAILY_STOP_LOSS:
            log.info("STOP diário")
            asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"stop_daily","text":text}))
            return
        if cooldown_active():
            log.info("COOLDOWN ativo")
            asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"cooldown","text":text}))
            return
        if streak_guard_triggered():
            start_cooldown()
            log.info("STREAK GUARD")
            asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"streak_guard","text":text}))
            return

    if not hourly_cap_ok():
        log.info("CAP por hora (aviso)")
        asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"hour_cap_notice","text":text}))
    if not is_top_hour_now():
        log.info("Fora da janela ouro (aviso)")
        asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"outside_window_notice","text":text}))

    # Score dos especialistas (não trava, só se score < SCORE_MIN)
    score = combined_score(text)
    if score < SCORE_MIN:
        log.info("SCORE baixo: %.2f < %.2f (descartado, sem travar o fluxo)", score, SCORE_MIN)
        asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"low_score","score":score,"text":text}))
        return

    # Anti-spam leve
    last = STATE.get("last_publish_ts")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (now_local() - last_dt).total_seconds() < MIN_GAP_SECS:
                log.info("ANTI-SPAM: gap %ss", MIN_GAP_SECS)
                asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"anti_spam","gap":MIN_GAP_SECS}))
                return
        except Exception:
            pass

    # Publica
    pretty = colorize_targets(text)
    STATE["open_signal"] = {"ts": now_local().isoformat(), "text": text, "pattern": pattern}
    STATE["last_publish_ts"] = now_local().isoformat()
    save_state()
    await tg_send(
        TARGET_CHAT_ID,
        f"🚀 <b>ENTRADA ABERTA (G0)</b>\n"
        f"✅ G0 {wr*100:.1f}% ({samples} am.) • score {score:.2f}\n"
        f"{pretty}"
    )
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

    STATE["recent_g0"].append(res)
    STATE["recent_g0"] = STATE["recent_g0"][-10:]
    if res == "R":
        STATE["daily_losses"] = STATE.get("daily_losses", 0) + 1

    # Contagem real
    if res == "G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] = STATE.get("streak_green", 0) + 1
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0

    # Ban por sequência de R
    if patt_hint:
        tail = pattern_tail(patt_hint, BAN_AFTER_CONSECUTIVE_R)
        if tail and tail.endswith("R"*BAN_AFTER_CONSECUTIVE_R):
            ban_pattern(patt_hint, f"{BAN_AFTER_CONSECUTIVE_R} REDS seguidos")

    # Resumo ao fechar
    if STATE.get("open_signal"):
        g = STATE["totals"]["greens"]; r = STATE["totals"]["reds"]
        total = g + r
        wr_day = (g/total*100.0) if total else 0.0
        resumo = (f"✅ {g} ⛔️ {r} 🎯 Acertamos {wr_day:.2f}%\n"
                  f"🥇 ESTAMOS A {STATE['streak_green']} GREENS SEGUIDOS ⏳")
        await tg_send(TARGET_CHAT_ID, resumo)
        STATE["open_signal"] = None

    asyncio.create_task(aux_log({
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

    cleanup_bans()
    bans = []
    for p, b in STATE.get("pattern_ban", {}).items():
        until = datetime.fromisoformat(b["until"]).strftime("%d/%m %H:%M")
        bans.append(f"• {p} (até {until})")

    lines = [
        f"<b>📊 Relatório Diário ({today_str()})</b>",
        f"Dia: <b>{g}G / {r}R</b> (WR: {wr_day:.1f}%)",
        f"Stop-loss: {DAILY_STOP_LOSS} • Perdas hoje: {STATE.get('daily_losses',0)}",
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
    return {"ok": True, "service": "g0-pipeline especialistas (sem travar)"}

# Aceita /webhook e /webhook/<segredo>; processa apenas channel_post e faz dedupe
@app.post("/webhook")
@app.post("/webhook/{secret}")
async def webhook(req: Request, secret: str|None=None):
    update = await req.json()
    if LOG_RAW: log.info("RAW UPDATE: %s", update)

    # Dedupe por update_id
    up_id = update.get("update_id")
    if up_id is not None:
        if up_id in STATE["seen_update_ids"]:
            return {"ok": True}
        STATE["seen_update_ids"].append(up_id)

    msg = update.get("channel_post") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or ""
    if not text or chat_id != SOURCE_CHAT_ID:
        return {"ok": True}

    # Dedupe por hash de texto (evita duplicado de reenvio)
    sig = text_sig(text)
    if sig in STATE["seen_text_sigs"]:
        return {"ok": True}
    STATE["seen_text_sigs"].append(sig)

    if FLOW_THROUGH:
        await tg_send(TARGET_CHAT_ID, colorize_targets(text))
        asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"mirror","text": text}))
        return {"ok": True}

    # Resultado?
    if GREEN_RE.search(text) or RED_RE.search(text):
        await process_result(text)
        return {"ok": True}

    # Potencial sinal?
    low = text.lower()
    if ("entrada confirmada" in low) or re.search(r"\b(banker|player|empate|bac\s*bo|fantan|dados)\b", low):
        await process_signal(text)
        return {"ok": True}

    asyncio.create_task(aux_log({"ts": now_local().isoformat(),"type":"other","text":text}))
    return {"ok": True}