import os, json, asyncio, re, math, pytz
from datetime import datetime, timedelta
from collections import deque, defaultdict
from dateutil import tz
from fastapi import FastAPI, Request
import httpx

# ====== CONFIG ======
TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
SOURCE_CHAT_ID = int(os.environ["SOURCE_CHAT_ID"])
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])

MIN_G0 = float(os.getenv("MIN_G0", "0.80"))

DAILY_STOP_LOSS = int(os.getenv("DAILY_STOP_LOSS", "3"))
STREAK_GUARD_LOSSES = int(os.getenv("STREAK_GUARD_LOSSES", "2"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "30"))
HOURLY_CAP = int(os.getenv("HOURLY_CAP", "6"))

TOP_HOURS_MIN_WINRATE = float(os.getenv("TOP_HOURS_MIN_WINRATE", "0.85"))
TOP_HOURS_COUNT = int(os.getenv("TOP_HOURS_COUNT", "6"))
TOP_HOURS_MIN_SAMPLES = int(os.getenv("TOP_HOURS_MIN_SAMPLES", "25"))

TZ_NAME = os.getenv("TZ", "UTC")
LOCAL_TZ = pytz.timezone(TZ_NAME)

STATE_PATH = os.getenv("STATE_PATH", "./state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI()

# ====== STATE (JSON) ======
STATE = {
    # log de mensagens (sinais e resultados do canal origem)
    "messages": [],  # {ts, type: 'signal'|'green'|'red', text, hour, pattern}
    # estatísticas por padrão (G0)
    "pattern_stats": {},  # pattern -> {"g0_green": int, "g0_total": int}
    # estatísticas por hora (UTC local) para G0
    "hour_stats": {},     # "00".."23" -> {"g0_green": int, "g0_total": int}
    # controle do dia
    "last_reset_date": None,  # YYYY-MM-DD na TZ local
    "daily_losses": 0,
    "hourly_entries": {},     # "YYYY-MM-DD HH" -> int
    # cooldown (streak guard)
    "cooldown_until": None,   # ISO
    # fila de últimos resultados G0 para detecção de sequencia
    "recent_g0": [],          # ex: ["G","R","G"]
}

def load_state():
    global STATE
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            STATE = json.load(f)
    except Exception:
        save_state()

def save_state():
    tmp = STATE_PATH + ".tmp"
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)

load_state()

# ====== UTILS ======
def now_local():
    return datetime.now(LOCAL_TZ)

def today_str():
    return now_local().strftime("%Y-%m-%d")

def hour_key(dt=None):
    if dt is None: dt = now_local()
    return dt.strftime("%Y-%m-%d %H")

async def tg_send(chat_id: int, text: str, disable_preview=True):
    async with httpx.AsyncClient(timeout=20) as cli:
        await cli.post(f"{API}/sendMessage",
                       json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": disable_preview})

# ====== EXTRAÇÃO DE PADRÕES ======
# Aceita formatos simples do canal: Banker, Player, Empate, combinações
PATTERN_RE = re.compile(r"(banker|player|empate)", re.I)

def extract_pattern(text: str) -> str | None:
    found = PATTERN_RE.findall(text or "")
    if not found:
        return None
    # Normaliza ordem/caixa
    norm = "+".join(sorted([w.strip().lower() for w in found]))
    return norm  # ex: "banker+empate" ou "player"

# ====== ESTATÍSTICA / ATUALIZAÇÃO ======
def add_message(msg_type: str, text: str, pattern: str | None):
    dt = now_local()
    entry = {
        "ts": dt.isoformat(),
        "type": msg_type,
        "text": text,
        "hour": dt.strftime("%H"),
        "pattern": pattern,
    }
    STATE["messages"].append(entry)
    save_state()

def register_outcome_g0(result: str, pattern_hint: str | None):
    # result: "G" | "R"
    if result not in ("G", "R"): return

    # Heurística: usa último sinal pendente OU pattern_hint extraído do texto GREEN/RED
    pattern = pattern_hint
    if not pattern:
        # procura do fim para o começo o ultimo sinal com pattern
        for m in reversed(STATE["messages"]):
            if m["type"] == "signal" and m.get("pattern"):
                pattern = m["pattern"]; break
    if not pattern:
        return  # sem pattern, não contabiliza

    ps = STATE["pattern_stats"].setdefault(pattern, {"g0_green": 0, "g0_total": 0})
    ps["g0_total"] += 1
    if result == "G": ps["g0_green"] += 1

    # por hora (usa hora do AGORA — aproximação)
    h = now_local().strftime("%H")
    hs = STATE["hour_stats"].setdefault(h, {"g0_green": 0, "g0_total": 0})
    hs["g0_total"] += 1
    if result == "G": hs["g0_green"] += 1

    # fila recente
    STATE["recent_g0"].append(result)
    STATE["recent_g0"] = STATE["recent_g0"][-10:]  # mantém últimos 10

    # daily losses
    if result == "R":
        STATE["daily_losses"] = STATE.get("daily_losses", 0) + 1

    save_state()

def g0_winrate(pattern: str) -> float:
    ps = STATE["pattern_stats"].get(pattern)
    if not ps or ps["g0_total"] == 0:
        return 0.0
    return ps["g0_green"] / ps["g0_total"]

def is_top_hour_now() -> bool:
    # calcula top horas pelo histórico (rolling simples)
    stats = STATE.get("hour_stats", {})
    # filtra por amostra mínima
    items = [(h, s) for h, s in stats.items() if s["g0_total"] >= TOP_HOURS_MIN_SAMPLES]
    if not items:
        # Sem dados -> libera (ou troque para False se quiser travar)
        return True
    # winrate por hora
    ranked = sorted(items, key=lambda kv: (kv[1]["g0_green"]/kv[1]["g0_total"]), reverse=True)
    top = []
    for h, s in ranked:
        wr = s["g0_green"]/s["g0_total"]
        if wr >= TOP_HOURS_MIN_WINRATE:
            top.append(h)
        if len(top) >= TOP_HOURS_COUNT:
            break
    current_h = now_local().strftime("%H")
    return current_h in top if top else True

def hourly_cap_ok() -> bool:
    key = hour_key()
    cnt = STATE["hourly_entries"].get(key, 0)
    return cnt < HOURLY_CAP

def register_hourly_entry():
    key = hour_key()
    STATE["hourly_entries"][key] = STATE["hourly_entries"].get(key, 0) + 1
    save_state()

def cooldown_active() -> bool:
    cu = STATE.get("cooldown_until")
    if not cu: return False
    return datetime.fromisoformat(cu) > now_local()

def start_cooldown():
    until = now_local() + timedelta(minutes=COOLDOWN_MINUTES)
    STATE["cooldown_until"] = until.isoformat()
    save_state()

def streak_guard_triggered() -> bool:
    recent = STATE.get("recent_g0", [])
    return len(recent) >= STREAK_GUARD_LOSSES and all(r == "R" for r in recent[-STREAK_GUARD_LOSSES:])

def daily_reset_if_needed():
    today = today_str()
    if STATE.get("last_reset_date") != today:
        STATE["last_reset_date"] = today
        STATE["daily_losses"] = 0
        STATE["hourly_entries"] = {}
        # cooldown é limpo a cada dia
        STATE["cooldown_until"] = None
        save_state()

# ====== ESPECIALISTAS (PIPELINE) ======
async def process_signal(text: str):
    daily_reset_if_needed()

    pattern = extract_pattern(text)
    add_message("signal", text, pattern)

    # Se não reconheceu padrão, não entra
    if not pattern:
        return

    # Especialista 1: Filtro G0
    wr = g0_winrate(pattern)
    if wr < MIN_G0:
        return  # descarta silencioso

    # Especialista 3: Janela de Ouro
    if not is_top_hour_now():
        return

    # Especialista 2: Sentinela de Risco
    if STATE["daily_losses"] >= DAILY_STOP_LOSS:
        return
    if cooldown_active():
        return
    if streak_guard_triggered():
        start_cooldown()
        return
    if not hourly_cap_ok():
        return

    # Publica
    out = f"✅ <b>G0 {wr*100:.1f}%</b>\n{text}"
    await tg_send(TARGET_CHAT_ID, out)
    register_hourly_entry()

async def process_result(text: str):
    # Heurística: GREEN/WIN = G ; LOSE/RED/PERDEMOS = R
    patt_hint = extract_pattern(text)
    t = text.lower()
    if "green" in t or "win" in t or "✅" in t:
        add_message("green", text, patt_hint)
        register_outcome_g0("G", patt_hint)
    elif "red" in t or "lose" in t or "perd" in t or "❌" in t:
        add_message("red", text, patt_hint)
        register_outcome_g0("R", patt_hint)

# ====== RELATÓRIO DIÁRIO ======
def build_daily_report():
    # resumo do dia
    today = today_str()
    # filtra mensagens do dia local
    msgs = [m for m in STATE["messages"] if m["ts"].startswith(today)]
    greens = sum(1 for m in msgs if m["type"] == "green")
    reds = sum(1 for m in msgs if m["type"] == "red")
    total = greens + reds
    wr_day = (greens/total) if total else 0.0

    # top padrões
    tops = []
    for p, s in STATE["pattern_stats"].items():
        if s["g0_total"] >= 5:
            wr = s["g0_green"]/s["g0_total"]
            tops.append((p, wr, s["g0_total"]))
    tops = sorted(tops, key=lambda x: x[1], reverse=True)[:5]

    # janelas de ouro
    hs = STATE.get("hour_stats", {})
    hours_rank = []
    for h, s in hs.items():
        if s["g0_total"] >= TOP_HOURS_MIN_SAMPLES:
            wr = s["g0_green"]/s["g0_total"]
            hours_rank.append((h, wr, s["g0_total"]))
    hours_rank.sort(key=lambda x: x[1], reverse=True)
    hours_rank = [x for x in hours_rank if x[1] >= TOP_HOURS_MIN_WINRATE][:TOP_HOURS_COUNT]

    txt = [
        "<b>📊 Relatório Diário (G0)</b>",
        f"Data: {today}",
        f"Resultado no dia: <b>{greens}G / {reds}R</b>  (WR: {wr_day*100:.1f}% | amostras: {total})",
        f"Stop-loss diário configurado: {DAILY_STOP_LOSS} | Perdas hoje: {STATE.get('daily_losses',0)}",
        "",
        "<b>Top padrões (histórico):</b>"
    ]
    if tops:
        txt += [f"• {p} → {wr*100:.1f}% ({n} jogos)" for p, wr, n in tops]
    else:
        txt.append("• Sem dados suficientes ainda.")

    txt += ["", "<b>Janelas de Ouro (por hora):</b>"]
    if hours_rank:
        txt += [f"• {h}h → {wr*100:.1f}% ({n} jogos)" for h, wr, n in hours_rank]
    else:
        txt.append("• Ainda aprendendo seus melhores horários…")

    return "\n".join(txt)

async def daily_report_loop():
    # loop simples que dispara relatório às 00:00 na TZ local
    await asyncio.sleep(5)
    while True:
        try:
            now = now_local()
            if now.hour == 0 and now.minute == 0:
                daily_reset_if_needed()
                rep = build_daily_report()
                await tg_send(TARGET_CHAT_ID, rep)
                # evita disparar várias vezes dentro do mesmo minuto
                await asyncio.sleep(65)
            else:
                await asyncio.sleep(10)
        except Exception as e:
            # não derruba o servidor
            await asyncio.sleep(5)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(daily_report_loop())

# ====== WEBHOOK ======
@app.get("/")
async def root():
    return {"ok": True, "service": "bacbo-g0-bot"}

@app.post("/webhook")
async def webhook(req: Request):
    update = await req.json()
    msg = update.get("message") or update.get("channel_post") or {}
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = msg.get("text", "") or ""

    # só processa origem
    if chat_id == SOURCE_CHAT_ID and text:
        low = text.lower()
        # classifica como sinal ou resultado por heurística
        if any(k in low for k in ["banker", "player", "empate", "bac bo", "dados"]):
            await process_signal(text)
        if any(k in low for k in ["green", "win", "✅", "red", "lose", "❌", "perd"]):
            await process_result(text)

    return {"ok": True}

# ====== ENDPOINT CRON MANUAL (opcional) ======
@app.get("/cron/daily_report")
async def cron_daily():
    daily_reset_if_needed()
    txt = build_daily_report()
    await tg_send(TARGET_CHAT_ID, txt)
    return {"ok": True, "sent": True}