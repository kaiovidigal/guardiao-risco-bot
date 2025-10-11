# app.py — TipMiner + Motor de Regras (últimos 3) • Gales • Cooldown • Heartbeat • Anti-cache • Retry • GREEN_RULE • Debug
import os, re, json, asyncio, pytz, logging, random, hashlib
from datetime import datetime, timedelta
from collections import deque
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

# ================== ENVs obrigatórias ==================
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])  # ex: -100...
TZ_NAME        = os.getenv("TZ", "UTC")
LOCAL_TZ       = pytz.timezone(TZ_NAME)

# ================== Config principal ==================
# Fechamento (externo / TipMiner)
REQUIRE_EXTERNAL_CONFIRM = os.getenv("REQUIRE_EXTERNAL_CONFIRM", "1") == "1"
MIN_RESULT_DELAY_SEC     = int(os.getenv("MIN_RESULT_DELAY_SEC", "8"))

# Regra de GREEN (se você segue ou entra no oposto)
GREEN_RULE = os.getenv("GREEN_RULE", "opposite").lower()  # opposite | follow

# Heartbeat e logs
HEARTBEAT_SEC   = int(os.getenv("HEARTBEAT_SEC", "3"))
DEBUG_TO_TARGET = os.getenv("DEBUG_TO_TARGET", "0") == "1"
LOG_RAW         = os.getenv("LOG_RAW", "1") == "1"

# Janelas / segurança
OPEN_TTL_SEC            = int(os.getenv("OPEN_TTL_SEC", "90"))
RESULT_GRACE_EXTEND_SEC = int(os.getenv("RESULT_GRACE_EXTEND_SEC", "25"))
MAX_OPEN_WINDOW_SEC     = int(os.getenv("MAX_OPEN_WINDOW_SEC", "150"))
CLOSE_STUCK_AFTER_SEC   = int(os.getenv("CLOSE_STUCK_AFTER_SEC", "180"))

# TipMiner / Fallback
TIPMINER_URL  = os.getenv("TIPMINER_URL", "https://www.tipminer.com/br/historico/jonbet/bac-bo")
FALLBACK_URL  = os.getenv("FALLBACK_URL", "https://casinoscores.com/pt-br/bac-bo/")
USE_PLAYWRIGHT = os.getenv("USE_PLAYWRIGHT", "0") == "1"  # deixe 0 por simplicidade

# Motor de Regras (estilo Selenium)
RULES_ENABLED            = os.getenv("RULES_ENABLED", "1") == "1"
RULES_MAX_GALES          = int(os.getenv("RULES_MAX_GALES", "2"))        # 0..2
RULES_COOLDOWN_ROUNDS    = int(os.getenv("RULES_COOLDOWN_ROUNDS", "3"))  # rodadas a pausar após LOSS
AUTO_OPEN_INTERVAL_SEC   = int(os.getenv("AUTO_OPEN_INTERVAL_SEC", "2")) # frequência do loop

# Misc
STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")
API        = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ================== App ==================
app = FastAPI()
log = logging.getLogger("uvicorn.error")

def now_local(): return datetime.now(LOCAL_TZ)
def colorize_side(s): return {"player":"🔵 Player", "banker":"🔴 Banker", "empate":"🟡 Empate"}.get(s or "", "?")

# ================== STATE ==================
STATE = {
    "totals": {"greens":0, "reds":0}, "streak_green": 0,
    "open_signal": None, "last_publish_ts": None,
    "processed_updates": deque(maxlen=500), "messages": [],
    # TipMiner snapshot control
    "auto_last_round_sig": None,  # assinatura da última rodada vista
    "auto_last_open_ts": None,
    # Regras (últimos 3 + alvo/gales + cooldown)
    "rules_recent3": deque(maxlen=3),   # valores 'Azul'/'Vermelho'/'Tie'
    "rules_target_side": None,          # 'player'/'banker'
    "rules_gale_stage": 0,              # 0=G0, 1=G1, 2=G2
    "rules_cooldown_rounds": 0,         # decrementa a cada virada de rodada
}
def _ensure_dir(p):
    d=os.path.dirname(p)
    if d and not os.path.exists(d): os.makedirs(d, exist_ok=True)

def _jsonable(o):
    if isinstance(o,deque): return list(o)
    if isinstance(o,dict): return {k:_jsonable(v) for k,v in o.items()}
    if isinstance(o,list): return [_jsonable(x) for x in o]
    return o

def save_state():
    _ensure_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(_jsonable(STATE), f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)

def load_state():
    global STATE
    try:
        with open(STATE_PATH,"r",encoding="utf-8") as f: data=json.load(f)
        data.setdefault("rules_recent3", [])
        STATE["rules_recent3"] = deque(data.get("rules_recent3", []), maxlen=3)
        for k, v in {
            "rules_target_side": None,
            "rules_gale_stage": 0,
            "rules_cooldown_rounds": 0,
            "auto_last_round_sig": None,
            "auto_last_open_ts": None,
            "open_signal": None,
            "last_publish_ts": None,
            "totals": {"greens":0,"reds":0},
            "streak_green": 0,
        }.items():
            STATE[k] = data.get(k, v)
    except Exception:
        save_state()
load_state()

async def tg_send(chat_id:int, text:str):
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            await cli.post(f"{API}/sendMessage",
                json={"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":True})
    except Exception as e:
        log.error("tg_send error: %s", e)

async def aux_log(e:dict):
    STATE["messages"].append(e)
    if len(STATE["messages"])>5000: STATE["messages"]=STATE["messages"][-4000:]
    save_state()

def _open_age_secs():
    osig = STATE.get("open_signal")
    if not osig: return 0
    try:
        opened = int(osig.get("src_opened_epoch") or 0)
        now_ep = int(now_local().timestamp())
        return max(0, now_ep - opened)
    except Exception:
        return 0

# ================== DETECÇÃO DE LADO no HTML ==================
TM_PLAYER_RE = re.compile(r"(🔵|🟦|Player|Azul)", re.I)
TM_BANKER_RE = re.compile(r"(🔴|🟥|Banker|Vermelho)", re.I)
CS_PLAYER_RE = re.compile(r"(Jogador|Player)", re.I)
CS_BANKER_RE = re.compile(r"(Banqueiro|Banker)", re.I)

# -------- FETCH ANTI-CACHE --------
async def _fetch(url: str) -> str | None:
    ts = int(now_local().timestamp())
    sep = "&" if "?" in url else "?"
    u = f"{url}{sep}_t={ts}"
    try:
        async with httpx.AsyncClient(timeout=15, headers={
            "User-Agent": "Mozilla/5.0",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Accept": "text/html,application/xhtml+xml"
        }) as cli:
            r = await cli.get(u)
            if r.status_code == 200 and r.text:
                return r.text
    except Exception as e:
        log.warning("fetch fail %s: %s", url, e)
    return None

def _pick_side_from_html(html: str, re_player: re.Pattern, re_banker: re.Pattern) -> str | None:
    if not html: return None
    head = html[:20000]
    has_p = bool(re_player.search(head)); has_b = bool(re_banker.search(head))
    if has_p and not has_b: return "player"
    if has_b and not has_p: return "banker"
    mp = re_player.search(head); mb = re_banker.search(head)
    if mp and mb: return "player" if mp.start() < mb.start() else "banker"
    return None

def _round_signature_from_html(html: str) -> str | None:
    if not html: return None
    head = html[:6000]
    return hashlib.sha1(head.encode("utf-8", errors="ignore")).hexdigest()

async def tipminer_snapshot() -> tuple[str | None, str | None]:
    # HTML simples (anti-cache)
    html = await _fetch(TIPMINER_URL)
    sig  = _round_signature_from_html(html or "")
    side = _pick_side_from_html(html or "", TM_PLAYER_RE, TM_BANKER_RE)
    if not side:
        fb_html = await _fetch(FALLBACK_URL)
        if fb_html:
            side = _pick_side_from_html(fb_html, CS_PLAYER_RE, CS_BANKER_RE)
            if not sig:
                sig = _round_signature_from_html(fb_html)
    # (Opcional) Playwright se habilitado
    if not side and USE_PLAYWRIGHT:
        try:
            from tipminer_scraper import get_tipminer_latest_side
            side2, _src, raw = await get_tipminer_latest_side(TIPMINER_URL)
            sig2 = _round_signature_from_html(raw or "")
            return sig2 or sig, side2
        except Exception as e:
            await aux_log({"ts": now_local().isoformat(), "type": "pw_err", "err": str(e)})
    return sig, side

# ================== Helpers do Motor de Regras ==================
def side_to_human(s):
    return "Azul" if s == "player" else ("Vermelho" if s == "banker" else "Tie")

def human_to_side(h):
    h = (h or "").lower()
    if h in ("azul","p","player"): return "player"
    if h in ("vermelho","b","banker"): return "banker"
    if h in ("tie","empate"): return "empate"
    return None

def _rules_should_fire(last3_human:list[str]) -> str | None:
    """Regras idênticas ao seu Selenium, recebe ['Azul'|'Vermelho'|'Tie'] x3."""
    if len(last3_human) < 3: 
        return None
    a = last3_human
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
    """Abre entrada (G0/G1/G2) no alvo das regras, usando publish_entry()."""
    if not RULES_ENABLED: 
        return
    if STATE.get("rules_cooldown_rounds", 0) > 0: 
        return
    tgt = STATE.get("rules_target_side")
    if not tgt or STATE.get("open_signal"):
        return
    await publish_entry(chosen_side=tgt, fonte_side=None,
                        msg={"message_id":0,"date":int(now_local().timestamp()),"text":"[rules]"})
    stage = STATE.get("rules_gale_stage", 0)
    if stage == 0:
        await tg_send(TARGET_CHAT_ID, "🚀 ENTRADA CONFIRMADA 🚀\n\nApostar no " + ("🔵" if tgt=="player" else "🔴"))
    else:
        await tg_send(TARGET_CHAT_ID, f"🔄 Gale {stage} no " + ("🔵" if tgt=="player" else "🔴"))
    save_state()

# ================== Resultado & Publicação ==================
def _result_from_sides(chosen: str, real: str) -> str:
    """GREEN conforme GREEN_RULE."""
    if not chosen or not real:
        return "R"
    if GREEN_RULE == "follow":
        return "G" if chosen == real else "R"
    # padrão: opposite
    return "G" if chosen != real else "R"

async def publish_entry(chosen_side:str, fonte_side:str|None, msg:dict):
    pretty = ("🚀 <b>ENTRADA AUTÔNOMA (G0/Gale)</b>\n"
              f"{colorize_side(chosen_side)}\n"
              f"Origem: TipMiner + Regras")
    await tg_send(TARGET_CHAT_ID, pretty)
    STATE["open_signal"] = {
        "ts": now_local().isoformat(),
        "chosen_side": chosen_side,
        "fonte_side": fonte_side,
        "expires_at": (now_local()+timedelta(seconds=OPEN_TTL_SEC)).isoformat(),
        "src_msg_id": msg.get("message_id", 0),
        "src_opened_epoch": msg.get("date", int(now_local().timestamp())),
    }
    # guarda assinatura da rodada na abertura
    try:
        html_open = await _fetch(TIPMINER_URL)
        STATE["open_signal"]["open_signature"] = _round_signature_from_html(html_open or "")
    except Exception:
        STATE["open_signal"]["open_signature"] = None

    STATE["last_publish_ts"] = now_local().isoformat()
    save_state()

async def announce_outcome(result:str, chosen_side:str|None):
    big = "🟩🟩🟩 <b>GREEN</b> 🟩🟩🟩" if result=="G" else "🟥🟥🟥 <b>LOSS</b> 🟥🟥🟥"
    await tg_send(TARGET_CHAT_ID, f"{big}\n⏱ {now_local().strftime('%H:%M:%S')}\nNossa: {colorize_side(chosen_side)}")
    g=STATE["totals"]["greens"]; r=STATE["totals"]["reds"]; t=g+r; wr=(g/t*100.0) if t else 0.0
    await tg_send(TARGET_CHAT_ID, f"📊 <b>Placar Geral</b>\n✅ {g}   ⛔️ {r}\n🎯 {wr:.2f}%  •  🔥 Streak {STATE['streak_green']}")

# ================== Fechamento externo (Retry + Logs) ==================
async def _apply_result_external_only(chosen_side: str | None) -> str | None:
    # modo SOS (somente se você setar REQUIRE_EXTERNAL_CONFIRM=0)
    if not REQUIRE_EXTERNAL_CONFIRM:
        return "G" if int(now_local().timestamp()) % 2 == 0 else "R"

    attempts = []
    for i in range(3):
        html = await _fetch(TIPMINER_URL)
        real = _pick_side_from_html(html or "", TM_PLAYER_RE, TM_BANKER_RE)
        attempts.append({"try": i+1, "site": "tipminer", "side": real})
        if not real:
            fb_html = await _fetch(FALLBACK_URL)
            real = _pick_side_from_html(fb_html or "", CS_PLAYER_RE, CS_BANKER_RE)
            attempts.append({"try": i+1, "site": "fallback", "side": real})
        if real:
            res = _result_from_sides(chosen_side, real)
            if DEBUG_TO_TARGET:
                await tg_send(TARGET_CHAT_ID, f"[DEBUG] try={i+1} real={real} -> {res} (rule={GREEN_RULE})")
            return res
        await asyncio.sleep(1.2)

    if DEBUG_TO_TARGET:
        await tg_send(TARGET_CHAT_ID, f"[DEBUG] sem cor no site após tentativas: {attempts}")
    await aux_log({"ts": now_local().isoformat(), "type": "external_missing", "attempts": attempts})
    return None

# ================== Fechar quando assinatura muda + delay mínimo ==================
async def maybe_close_by_external():
    osig = STATE.get("open_signal")
    if not osig:
        return

    # assinatura mudou? rodada antiga foi resolvida
    signature_changed = False
    try:
        html_now = await _fetch(TIPMINER_URL)
        sig_now  = _round_signature_from_html(html_now or "")
        sig_open = osig.get("open_signature")
        signature_changed = (sig_open and sig_now and sig_open != sig_now)
    except Exception:
        pass

    opened_epoch = osig.get("src_opened_epoch", 0)
    now_epoch = int(now_local().timestamp())
    if not signature_changed and opened_epoch and (now_epoch - opened_epoch) < MIN_RESULT_DELAY_SEC:
        return

    final_res = await _apply_result_external_only(osig.get("chosen_side"))
    if final_res is None:
        if signature_changed and DEBUG_TO_TARGET:
            await tg_send(TARGET_CHAT_ID, "[DEBUG] rodada virou, aguardando cor no próximo heartbeat")
        return

    # aplica placar geral
    if final_res == "G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] += 1
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0

    await announce_outcome(final_res, osig.get("chosen_side"))

    # ======= Gales/Cooldown (encadeamento de regras) =======
    if final_res == "G":
        STATE["rules_target_side"] = None
        STATE["rules_gale_stage"] = 0
    else:
        stage = STATE.get("rules_gale_stage", 0)
        tgt   = STATE.get("rules_target_side")
        if RULES_ENABLED and tgt and stage < RULES_MAX_GALES:
            STATE["rules_gale_stage"] = stage + 1  # próximo gale
            save_state()
            # reabre gale agora
            await _rules_try_open_signal()
        else:
            # encerra sequência e entra em cooldown
            STATE["rules_target_side"] = None
            STATE["rules_gale_stage"] = 0
            STATE["rules_cooldown_rounds"] = RULES_COOLDOWN_ROUNDS

    # fecha sinal atual
    STATE["open_signal"] = None
    save_state()
    if DEBUG_TO_TARGET:
        await tg_send(TARGET_CHAT_ID, f"[HB] Fechado (sig_changed={signature_changed}) -> {final_res}")

# ================== TTL / Anti-trava ==================
async def expire_open_if_needed():
    osig = STATE.get("open_signal")
    if not osig: return
    if _open_age_secs() >= CLOSE_STUCK_AFTER_SEC:
        STATE["open_signal"] = None
        save_state(); return
    exp = osig.get("expires_at")
    if not exp: return
    if datetime.fromisoformat(exp) > now_local(): return
    STATE["open_signal"] = None
    save_state()
    await tg_send(TARGET_CHAT_ID, "⏳ Encerrado por timeout (TTL) — descartado")

# ================== Loop TipMiner + Motor de Regras ==================
async def auto_loop():
    while True:
        try:
            sig, last_side = await tipminer_snapshot()
            if sig:
                prev_sig = STATE.get("auto_last_round_sig")
                # rodou uma nova bola?
                if prev_sig != sig:
                    STATE["auto_last_round_sig"] = sig
                    # empilha último lado humano
                    if last_side:
                        STATE["rules_recent3"].append(side_to_human(last_side))
                    # controla cooldown
                    if STATE.get("rules_cooldown_rounds", 0) > 0:
                        STATE["rules_cooldown_rounds"] -= 1
                    else:
                        # se não há sinal aberto e não estamos em gale pendente, tenta disparar regra
                        if RULES_ENABLED and STATE.get("open_signal") is None and STATE.get("rules_gale_stage",0) == 0:
                            target_human = _rules_should_fire(list(STATE["rules_recent3"]))
                            if target_human:
                                STATE["rules_target_side"] = human_to_side(target_human)
                                STATE["rules_gale_stage"] = 0
                                await _rules_try_open_signal()
                    save_state()
        except Exception as e:
            try:
                await aux_log({"ts": now_local().isoformat(), "type": "auto_err", "err": str(e)})
            except:
                pass
        await asyncio.sleep(AUTO_OPEN_INTERVAL_SEC)

# ================== Heartbeat ==================
async def heartbeat_loop():
    while True:
        try:
            await maybe_close_by_external()
            await expire_open_if_needed()
        except Exception as e:
            try:
                await aux_log({"ts": now_local().isoformat(), "type": "hb_err", "err": str(e)})
            except:
                pass
        await asyncio.sleep(HEARTBEAT_SEC)

@app.on_event("startup")
async def _start_background_tasks():
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(auto_loop())

# ================== FastAPI / Rotas ==================
@app.middleware("http")
async def safe_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        logging.exception("Middleware error: %s", e)
        return JSONResponse({"ok": True}, status_code=200)

@app.get("/")
async def root():
    return {"ok": True, "service": "TipMiner + Regras (últimos 3) • Gales • Cooldown • Heartbeat • Anti-cache • Retry • GREEN_RULE"}

@app.get("/debug/tipminer")
async def dbg_tm():
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
        "last_publish_ts": STATE.get("last_publish_ts")
    }

# ---- Force-close (teste manual) ----
@app.get("/debug/force-close")
async def dbg_force_close():
    osig = STATE.get("open_signal")
    if not osig:
        return {"ok": False, "err": "no open_signal"}
    res = await _apply_result_external_only(osig.get("chosen_side"))
    if not res:
        return {"ok": False, "err": "no external color"}
    if res == "G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] += 1
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0
    await announce_outcome(res, osig.get("chosen_side"))
    # gales/cooldown como no fechamento normal:
    if res == "G":
        STATE["rules_target_side"] = None
        STATE["rules_gale_stage"] = 0
    else:
        stage = STATE.get("rules_gale_stage", 0)
        tgt   = STATE.get("rules_target_side")
        if RULES_ENABLED and tgt and stage < RULES_MAX_GALES:
            STATE["rules_gale_stage"] = stage + 1
            save_state()
            await _rules_try_open_signal()
        else:
            STATE["rules_target_side"] = None
            STATE["rules_gale_stage"] = 0
            STATE["rules_cooldown_rounds"] = RULES_COOLDOWN_ROUNDS
    STATE["open_signal"] = None
    save_state()
    if DEBUG_TO_TARGET:
        await tg_send(TARGET_CHAT_ID, f"[DEBUG] force-close: {res}")
    return {"ok": True, "result": res}