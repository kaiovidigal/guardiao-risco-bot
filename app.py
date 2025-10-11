# TipMiner-only bot — abre e fecha com base no histórico do TipMiner
# Recursos: regras últimos-3, gales, cooldown, heartbeat, anti-cache, debug

import os, re, json, asyncio, pytz, logging, hashlib, random
from datetime import datetime, timedelta
from collections import deque

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx

# ====== ENVs obrigatórias ======
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])  # ex: -100xxxxxxxx
TZ_NAME        = os.getenv("TZ", "UTC")
LOCAL_TZ       = pytz.timezone(TZ_NAME)

# ====== Config ======
TIPMINER_URL  = os.getenv("TIPMINER_URL", "https://www.tipminer.com/br/historico/jonbet/bac-bo")
FALLBACK_URL  = os.getenv("FALLBACK_URL", "https://casinoscores.com/pt-br/bac-bo/")

HEARTBEAT_SEC   = int(os.getenv("HEARTBEAT_SEC", "2"))
AUTO_OPEN_INTERVAL_SEC = int(os.getenv("AUTO_OPEN_INTERVAL_SEC", "2"))
MIN_RESULT_DELAY_SEC   = int(os.getenv("MIN_RESULT_DELAY_SEC", "5"))

OPEN_TTL_SEC          = int(os.getenv("OPEN_TTL_SEC", "90"))
CLOSE_STUCK_AFTER_SEC = int(os.getenv("CLOSE_STUCK_AFTER_SEC", "180"))
RESULT_GRACE_EXTEND_SEC = int(os.getenv("RESULT_GRACE_EXTEND_SEC","25"))

RULES_ENABLED         = os.getenv("RULES_ENABLED","1") == "1"
RULES_MAX_GALES       = int(os.getenv("RULES_MAX_GALES","2"))
RULES_COOLDOWN_ROUNDS = int(os.getenv("RULES_COOLDOWN_ROUNDS","3"))

GREEN_RULE            = os.getenv("GREEN_RULE","opposite").lower()  # opposite | follow
DEBUG_TO_TARGET       = os.getenv("DEBUG_TO_TARGET","1") == "1"

STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ====== App ======
app = FastAPI()
log = logging.getLogger("uvicorn.error")

def now_local(): return datetime.now(LOCAL_TZ)
def colorize_side(s): return {"player":"🔵 Player","banker":"🔴 Banker","empate":"🟡 Empate"}.get(s or "", "?")

# ====== Estado ======
STATE = {
    "totals": {"greens":0,"reds":0},
    "streak_green": 0,
    "open_signal": None,
    "auto_last_round_sig": None,   # assinatura da última rodada do site
    "rules_recent3": deque(maxlen=3),   # lista de textos: Azul/Vermelho/Tie
    "rules_target_side": None,     # 'player'|'banker'
    "rules_gale_stage": 0,         # 0..2
    "rules_cooldown_rounds": 0,
}

def _ensure_dir(p):
    d=os.path.dirname(p)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _jsonable(o):
    if isinstance(o,deque): return list(o)
    if isinstance(o,dict): return {k:_jsonable(v) for k,v in o.items()}
    if isinstance(o,list): return [_jsonable(x) for x in o]
    return o

def save_state():
    _ensure_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_jsonable(STATE), f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)

def load_state():
    global STATE
    try:
        with open(STATE_PATH,"r",encoding="utf-8") as f:
            data=json.load(f)
        STATE["totals"]             = data.get("totals", {"greens":0,"reds":0})
        STATE["streak_green"]       = data.get("streak_green", 0)
        STATE["open_signal"]        = data.get("open_signal", None)
        STATE["auto_last_round_sig"]= data.get("auto_last_round_sig", None)
        STATE["rules_recent3"]      = deque(data.get("rules_recent3", []), maxlen=3)
        STATE["rules_target_side"]  = data.get("rules_target_side", None)
        STATE["rules_gale_stage"]   = data.get("rules_gale_stage", 0)
        STATE["rules_cooldown_rounds"]= data.get("rules_cooldown_rounds", 0)
    except Exception:
        save_state()
load_state()

async def tg_send(chat_id:int, text:str):
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            await cli.post(f"{API}/sendMessage",
                           json={"chat_id":chat_id,"text":text,"parse_mode":"HTML",
                                 "disable_web_page_preview":True})
    except Exception as e:
        log.error("tg_send error: %s", e)

def _open_age_secs():
    osig = STATE.get("open_signal") or {}
    opened = int(osig.get("src_opened_epoch") or 0)
    if not opened: return 0
    return max(0, int(now_local().timestamp()) - opened)

# ====== Parsers do TipMiner ======
TM_PLAYER_RE = re.compile(r"(🔵|🟦|Player|Azul)", re.I)
TM_BANKER_RE = re.compile(r"(🔴|🟥|Banker|Vermelho)", re.I)

CS_PLAYER_RE = re.compile(r"(Jogador|Player)", re.I)   # fallback
CS_BANKER_RE = re.compile(r"(Banqueiro|Banker)", re.I)

async def _fetch(url: str) -> str | None:
    # anti-cache simples
    ts = int(now_local().timestamp())
    sep = "&" if "?" in url else "?"
    u = f"{url}{sep}_t={ts}"
    try:
        async with httpx.AsyncClient(timeout=15, headers={
            "User-Agent":"Mozilla/5.0",
            "Cache-Control":"no-cache",
            "Pragma":"no-cache",
            "Accept":"text/html,application/xhtml+xml"
        }) as cli:
            r = await cli.get(u)
            if r.status_code == 200 and r.text:
                return r.text
    except Exception as e:
        log.warning("fetch fail %s: %s", url, e)
    return None

def _pick_side_from_html(html: str, re_p: re.Pattern, re_b: re.Pattern) -> str | None:
    if not html: return None
    head = html[:20000]
    hp, hb = bool(re_p.search(head)), bool(re_b.search(head))
    if hp and not hb: return "player"
    if hb and not hp: return "banker"
    mp, mb = re_p.search(head), re_b.search(head)
    if mp and mb: return "player" if mp.start() < mb.start() else "banker"
    return None

def _round_signature_from_html(html: str) -> str | None:
    if not html: return None
    head = html[:6000]
    return hashlib.sha1(head.encode("utf-8","ignore")).hexdigest()

async def tipminer_snapshot() -> tuple[str | None, str | None]:
    html = await _fetch(TIPMINER_URL)
    sig  = _round_signature_from_html(html or "")
    side = _pick_side_from_html(html or "", TM_PLAYER_RE, TM_BANKER_RE)
    if not side:
        fb = await _fetch(FALLBACK_URL)
        if fb:
            side = _pick_side_from_html(fb, CS_PLAYER_RE, CS_BANKER_RE)
            if not sig:
                sig = _round_signature_from_html(fb)
    return sig, side

# ====== Regras (últimos 3) ======
def side_to_human(s):
    return "Azul" if s=="player" else ("Vermelho" if s=="banker" else "Tie")
def human_to_side(h):
    t=(h or "").lower()
    if t in ("azul","p","player"): return "player"
    if t in ("vermelho","b","banker"): return "banker"
    if t in ("tie","empate"): return "empate"
    return None

def rules_should_fire(last3:list[str]) -> str | None:
    if len(last3) < 3: return None
    a = last3
    if a == ['Vermelho','Vermelho','Vermelho']: return 'Azul'
    if a == ['Azul','Azul','Azul']:             return 'Vermelho'
    if a == ['Tie','Vermelho','Tie']:           return 'Azul'
    if a == ['Tie','Azul','Tie']:               return 'Vermelho'
    if a == ['Vermelho','Tie','Tie']:           return 'Azul'
    if a == ['Azul','Tie','Tie']:               return 'Vermelho'
    if a == ['Vermelho','Tie','Vermelho']:      return 'Azul'
    if a == ['Azul','Tie','Azul']:              return 'Vermelho'
    return None

async def _rules_try_open_signal():
    if not RULES_ENABLED: return
    if STATE.get("rules_cooldown_rounds", 0) > 0: return
    if STATE.get("open_signal"): return
    tgt = STATE.get("rules_target_side")
    if not tgt: return
    await publish_entry(tgt)

# ====== Publicação / Resultado ======
async def publish_entry(chosen_side: str):
    await tg_send(TARGET_CHAT_ID,
                  "🚀 <b>ENTRADA AUTÔNOMA</b>\n"
                  f"{colorize_side(chosen_side)}\n"
                  "Origem: TipMiner + Regras")
    STATE["open_signal"] = {
        "ts": now_local().isoformat(),
        "chosen_side": chosen_side,
        "expires_at": (now_local()+timedelta(seconds=OPEN_TTL_SEC)).isoformat(),
        "src_opened_epoch": int(now_local().timestamp()),
        "open_signature": None
    }
    # guarda assinatura da rodada no momento da abertura
    try:
        html = await _fetch(TIPMINER_URL)
        STATE["open_signal"]["open_signature"] = _round_signature_from_html(html or "")
    except:
        STATE["open_signal"]["open_signature"] = None
    save_state()

def result_from_sides(chosen: str, real: str) -> str:
    if GREEN_RULE == "follow":
        return "G" if chosen == real else "R"
    # opposite
    return "G" if chosen != real else "R"

async def announce_outcome(res:str, chosen:str):
    await tg_send(TARGET_CHAT_ID,
                  ("🟩🟩🟩 <b>GREEN</b> 🟩🟩🟩" if res=="G" else "🟥🟥🟥 <b>LOSS</b> 🟥🟥🟥") +
                  f"\n⏱ {now_local().strftime('%H:%M:%S')}\nNossa: {colorize_side(chosen)}")
    g=STATE["totals"]["greens"]; r=STATE["totals"]["reds"]; t=g+r; wr=(g/t*100.0) if t else 0.0
    await tg_send(TARGET_CHAT_ID, f"📊 <b>Placar Geral</b>\n✅ {g}   ⛔️ {r}\n🎯 {wr:.2f}%  •  🔥 Streak {STATE['streak_green']}")

async def apply_external_close(chosen_side: str) -> str | None:
    # 3 tentativas: TipMiner -> Fallback
    for i in range(3):
        html = await _fetch(TIPMINER_URL)
        real = _pick_side_from_html(html or "", TM_PLAYER_RE, TM_BANKER_RE)
        if not real:
            fb = await _fetch(FALLBACK_URL)
            real = _pick_side_from_html(fb or "", CS_PLAYER_RE, CS_BANKER_RE)
        if real:
            res = result_from_sides(chosen_side, real)
            if DEBUG_TO_TARGET:
                await tg_send(TARGET_CHAT_ID, f"[DEBUG] fechamento try={i+1} real={real} => {res}")
            return res
        await asyncio.sleep(1.0)
    if DEBUG_TO_TARGET:
        await tg_send(TARGET_CHAT_ID, "[DEBUG] sem cor no site para fechar")
    return None

async def maybe_close_by_site():
    osig = STATE.get("open_signal")
    if not osig: return
    # assinatura mudou (nova rodada) OU delay mínimo atingido?
    sig_open = osig.get("open_signature")
    html_now = await _fetch(TIPMINER_URL)
    sig_now  = _round_signature_from_html(html_now or "")
    signature_changed = (sig_open and sig_now and sig_open != sig_now)

    if not signature_changed:
        if int(now_local().timestamp()) - osig.get("src_opened_epoch", 0) < MIN_RESULT_DELAY_SEC:
            return

    final_res = await apply_external_close(osig.get("chosen_side"))
    if not final_res:
        return

    # aplicar placar
    if final_res == "G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] += 1
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0
    await announce_outcome(final_res, osig.get("chosen_side"))

    # encadear gales / cooldown
    if final_res == "G":
        STATE["rules_target_side"] = None
        STATE["rules_gale_stage"]  = 0
    else:
        stage = STATE.get("rules_gale_stage", 0)
        tgt   = STATE.get("rules_target_side")
        if RULES_ENABLED and tgt and stage < RULES_MAX_GALES:
            STATE["rules_gale_stage"] = stage + 1
            save_state()
            await _rules_try_open_signal()
        else:
            STATE["rules_target_side"] = None
            STATE["rules_gale_stage"]  = 0
            STATE["rules_cooldown_rounds"] = RULES_COOLDOWN_ROUNDS

    STATE["open_signal"] = None
    save_state()

async def expire_open_if_needed():
    osig = STATE.get("open_signal")
    if not osig: return
    if _open_age_secs() >= CLOSE_STUCK_AFTER_SEC:
        STATE["open_signal"] = None; save_state(); return
    exp = osig.get("expires_at")
    if exp and datetime.fromisoformat(exp) <= now_local():
        STATE["open_signal"] = None; save_state()
        await tg_send(TARGET_CHAT_ID, "⏳ Encerrado por timeout (TTL) — descartado")

# ====== Loops ======
async def auto_loop():
    while True:
        try:
            sig, last_side = await tipminer_snapshot()
            if sig and sig != STATE.get("auto_last_round_sig"):
                STATE["auto_last_round_sig"] = sig
                if last_side:
                    STATE["rules_recent3"].append(side_to_human(last_side))
                # cooldown
                if STATE.get("rules_cooldown_rounds",0) > 0:
                    STATE["rules_cooldown_rounds"] -= 1
                else:
                    if RULES_ENABLED and STATE.get("open_signal") is None and STATE.get("rules_gale_stage",0) == 0:
                        target_human = rules_should_fire(list(STATE["rules_recent3"]))
                        if target_human:
                            STATE["rules_target_side"] = human_to_side(target_human)
                            STATE["rules_gale_stage"]  = 0
                            await _rules_try_open_signal()
                save_state()
        except Exception as e:
            log.error("auto_loop err: %s", e)
        await asyncio.sleep(AUTO_OPEN_INTERVAL_SEC)

async def heartbeat_loop():
    while True:
        try:
            await maybe_close_by_site()
            await expire_open_if_needed()
        except Exception as e:
            log.error("hb err: %s", e)
        await asyncio.sleep(HEARTBEAT_SEC)

@app.on_event("startup")
async def _start_tasks():
    asyncio.create_task(auto_loop())
    asyncio.create_task(heartbeat_loop())

# ====== HTTP ======
@app.middleware("http")
async def safe_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        logging.exception("Middleware error: %s", e)
        return JSONResponse({"ok": True}, status_code=200)

@app.get("/")
async def root():
    return {"ok": True, "service": "TipMiner 100% • Regras(3) • Gales • Cooldown • Heartbeat"}

@app.get("/debug/tipminer")
async def dbg_tipminer():
    try:
        sig, last_side = await tipminer_snapshot()
        return {"ok": True, "signature": sig, "last_side": last_side, "recent3": list(STATE["rules_recent3"])}
    except Exception as e:
        return {"ok": False, "err": str(e)}

@app.get("/debug/state")
async def dbg_state():
    return {
        "open_signal": STATE.get("open_signal"),
        "gales": STATE.get("rules_gale_stage"),
        "target": STATE.get("rules_target_side"),
        "cooldown_rounds": STATE.get("rules_cooldown_rounds"),
    }