# app.py — 100% TipMiner (abre + fecha) • Regras(3) • Gales • Cooldown • Heartbeat • Anti-cache
import os, re, json, asyncio, pytz, logging, hashlib
from datetime import datetime, timedelta
from collections import deque
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx

# ========= ENVs obrigatórias =========
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]          # token do bot do Telegram
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])   # ex.: -100xxxxxxxxxxxx
TZ_NAME        = os.getenv("TZ", "America/Sao_Paulo")
LOCAL_TZ       = pytz.timezone(TZ_NAME)

# ========= Fontes =========
TIPMINER_URL  = os.getenv("TIPMINER_URL", "https://www.tipminer.com/br/historico/jonbet/bac-bo")
FALLBACK_URL  = os.getenv("FALLBACK_URL", "https://casinoscores.com/pt-br/bac-bo/")

# ========= Timings / Janelas =========
HEARTBEAT_SEC           = int(os.getenv("HEARTBEAT_SEC", "2"))
AUTO_OPEN_INTERVAL_SEC  = int(os.getenv("AUTO_OPEN_INTERVAL_SEC","2"))
MIN_RESULT_DELAY_SEC    = int(os.getenv("MIN_RESULT_DELAY_SEC", "6"))   # mínimo após abrir
OPEN_TTL_SEC            = int(os.getenv("OPEN_TTL_SEC", "90"))
CLOSE_STUCK_AFTER_SEC   = int(os.getenv("CLOSE_STUCK_AFTER_SEC","180"))
RESULT_GRACE_EXTEND_SEC = int(os.getenv("RESULT_GRACE_EXTEND_SEC","25"))
DEBUG_TO_TARGET         = os.getenv("DEBUG_TO_TARGET","1") == "1"

# ========= Motor de Regras =========
RULES_ENABLED         = os.getenv("RULES_ENABLED","1") == "1"
RULES_MAX_GALES       = int(os.getenv("RULES_MAX_GALES","2"))            # 0..2
RULES_COOLDOWN_ROUNDS = int(os.getenv("RULES_COOLDOWN_ROUNDS","3"))      # pausas após LOSS

# Como conta GREEN: 'opposite' (acerta quando real != escolhido) ou 'follow'
GREEN_RULE = os.getenv("GREEN_RULE","opposite").lower()

# ========= Infra =========
API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
STATE_PATH = os.getenv("STATE_PATH", "./state/state.json")

app = FastAPI()
log = logging.getLogger("uvicorn.error")
def now_local(): return datetime.now(LOCAL_TZ)

# ========= Estado =========
STATE = {
    "totals": {"greens":0,"reds":0}, "streak_green": 0,
    "open_signal": None,                 # entrada aberta
    "auto_last_round_sig": None,         # assinatura da última rodada do site
    "rules_recent3": deque(maxlen=3),    # "Azul"/"Vermelho"/"Tie"
    "rules_target_side": None,           # 'player'/'banker'
    "rules_gale_stage": 0,               # 0=G0,1=G1,2=G2
    "rules_cooldown_rounds": 0,
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
    try:
        with open(STATE_PATH,"r",encoding="utf-8") as f: data=json.load(f)
        STATE["rules_recent3"] = deque(data.get("rules_recent3", []), maxlen=3)
        for k in STATE:
            if k!="rules_recent3":
                STATE[k]=data.get(k, STATE[k])
    except Exception:
        save_state()
load_state()

# ========= Telegram =========
async def tg_send(chat_id:int, text:str):
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            await cli.post(f"{API}/sendMessage",
                json={"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":True})
    except Exception as e:
        log.error("tg_send error: %s", e)

def colorize_side(s): 
    return {"player":"🔵 Player","banker":"🔴 Banker","empate":"🟡 Empate"}.get(s or "", "?")

# ========= TipMiner parsing =========
TM_PLAYER_RE = re.compile(r"(🔵|🟦|Azul|Player|Jogador)", re.I)
TM_BANKER_RE = re.compile(r"(🔴|🟥|Vermelho|Banker|Banqueiro)", re.I)
CS_PLAYER_RE = re.compile(r"(Azul|Player|Jogador)", re.I)        # fallback
CS_BANKER_RE = re.compile(r"(Vermelho|Banker|Banqueiro)", re.I)

async def _fetch(url: str) -> str | None:
    ts = int(now_local().timestamp())
    sep = "&" if "?" in url else "?"
    u = f"{url}{sep}_t={ts}"  # anti-cache
    try:
        async with httpx.AsyncClient(timeout=15, headers={
            "User-Agent":"Mozilla/5.0",
            "Cache-Control":"no-cache, no-store, must-revalidate",
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
    head = html[:24000]
    hp, hb = bool(re_p.search(head)), bool(re_b.search(head))
    if hp and not hb: return "player"
    if hb and not hp: return "banker"
    mp, mb = re_p.search(head), re_b.search(head)
    if mp and mb: return "player" if mp.start() < mb.start() else "banker"
    return None

def _round_signature_from_html(html: str) -> str | None:
    if not html: return None
    head = html[:8000]
    return hashlib.sha1(head.encode("utf-8","ignore")).hexdigest()

async def tipminer_snapshot() -> tuple[str|None, str|None]:
    html = await _fetch(TIPMINER_URL)
    sig  = _round_signature_from_html(html or "")
    side = _pick_side_from_html(html or "", TM_PLAYER_RE, TM_BANKER_RE)
    if not side:
        fb = await _fetch(FALLBACK_URL)
        if fb:
            side = _pick_side_from_html(fb, CS_PLAYER_RE, CS_BANKER_RE)
            if not sig:
                sig = _round_signature_from_html(fb or "")
    return sig, side  # "player"|"banker"|None

# ========= Regras =========
def side_to_human(s):
    return "Azul" if s=="player" else ("Vermelho" if s=="banker" else "Tie")
def human_to_side(h):
    t=(h or "").lower()
    if t in ("azul","p","player","jogador"): return "player"
    if t in ("vermelho","b","banker","banqueiro"): return "banker"
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

def _result_from_sides(chosen: str, real: str) -> str:
    if GREEN_RULE == "follow":   # acerta se for igual
        return "G" if chosen == real else "R"
    return "G" if chosen != real else "R"   # opposite (padrão)

def _open_age_secs():
    osig = STATE.get("open_signal")
    if not osig: return 0
    try:
        return max(0, int(now_local().timestamp()) - int(osig.get("src_opened_epoch") or 0))
    except: return 0

# ========= Publicação =========
async def publish_entry(chosen_side: str):
    await tg_send(TARGET_CHAT_ID,
        "🚀 <b>ENTRADA AUTÔNOMA</b>\n"
        f"{colorize_side(chosen_side)}\n"
        "Origem: TipMiner + Regras(3)")
    STATE["open_signal"] = {
        "ts": now_local().isoformat(),
        "chosen_side": chosen_side,
        "expires_at": (now_local()+timedelta(seconds=OPEN_TTL_SEC)).isoformat(),
        "src_opened_epoch": int(now_local().timestamp()),
        "open_signature": None
    }
    # guarda assinatura no momento da abertura
    try:
        html = await _fetch(TIPMINER_URL)
        STATE["open_signal"]["open_signature"] = _round_signature_from_html(html or "")
    except:
        STATE["open_signal"]["open_signature"] = None
    save_state()

async def announce_outcome(result:str, chosen_side:str):
    big = "🟩🟩🟩 <b>GREEN</b> 🟩🟩🟩" if result=="G" else "🟥🟥🟥 <b>LOSS</b> 🟥🟥🟥"
    await tg_send(TARGET_CHAT_ID, f"{big}\n⏱ {now_local().strftime('%H:%M:%S')}\nNossa: {colorize_side(chosen_side)}")
    g=STATE["totals"]["greens"]; r=STATE["totals"]["reds"]; t=g+r; wr=(g/t*100.0) if t else 0.0
    await tg_send(TARGET_CHAT_ID, f"📊 <b>Placar Geral</b>\n✅ {g}   ⛔️ {r}\n🎯 {wr:.2f}%  •  🔥 Streak {STATE['streak_green']}")

# ========= Abertura (regras + gale) =========
async def rules_try_open():
    if not RULES_ENABLED: return
    if STATE.get("rules_cooldown_rounds",0)>0: return
    if STATE.get("open_signal"): return
    tgt = STATE.get("rules_target_side")
    if not tgt: return
    stage = STATE.get("rules_gale_stage",0)
    await publish_entry(tgt)
    if stage==0:
        await tg_send(TARGET_CHAT_ID, "🚀 ENTRADA CONFIRMADA\nApostar no " + ("🔵" if tgt=="player" else "🔴"))
    else:
        await tg_send(TARGET_CHAT_ID, f"🔄 Gale {stage} no " + ("🔵" if tgt=="player" else "🔴"))

# ========= Fechamento (puxando cor real do site) =========
async def _apply_external_close(chosen:str) -> str | None:
    for i in range(4):
        html = await _fetch(TIPMINER_URL)
        real = _pick_side_from_html(html or "", TM_PLAYER_RE, TM_BANKER_RE)
        if not real:
            fb = await _fetch(FALLBACK_URL)
            real = _pick_side_from_html(fb or "", CS_PLAYER_RE, CS_BANKER_RE)
        if real:
            res = _result_from_sides(chosen, real)
            if DEBUG_TO_TARGET:
                await tg_send(TARGET_CHAT_ID, f"[DEBUG] try={i+1} real={real} -> {res} (rule={GREEN_RULE})")
            return res
        await asyncio.sleep(1.0)
    if DEBUG_TO_TARGET:
        await tg_send(TARGET_CHAT_ID, "[DEBUG] sem cor no site para fechar")
    return None

async def maybe_close_by_site():
    osig = STATE.get("open_signal")
    if not osig: return
    # rodada virou? (assinatura mudou) OU já passou delay mínimo?
    sig_open = osig.get("open_signature")
    sig_now = None
    try:
        html_now = await _fetch(TIPMINER_URL)
        sig_now  = _round_signature_from_html(html_now or "")
    except: pass
    signature_changed = bool(sig_open and sig_now and sig_open != sig_now)

    if not signature_changed:
        if int(now_local().timestamp()) - osig.get("src_opened_epoch", 0) < MIN_RESULT_DELAY_SEC:
            return

    final = await _apply_external_close(osig["chosen_side"])
    if not final:
        return

    # aplica placar e encadeia lógica de gales/cooldown
    if final=="G":
        STATE["totals"]["greens"] += 1
        STATE["streak_green"] += 1
        STATE["rules_target_side"] = None
        STATE["rules_gale_stage"]  = 0
    else:
        STATE["totals"]["reds"] += 1
        STATE["streak_green"] = 0
        stage = STATE.get("rules_gale_stage",0)
        tgt   = STATE.get("rules_target_side")
        if RULES_ENABLED and tgt and stage < RULES_MAX_GALES:
            STATE["rules_gale_stage"] = stage + 1
            save_state()
            await rules_try_open()   # abre próximo gale
        else:
            STATE["rules_target_side"] = None
            STATE["rules_gale_stage"]  = 0
            STATE["rules_cooldown_rounds"] = RULES_COOLDOWN_ROUNDS

    await announce_outcome(final, osig["chosen_side"])
    STATE["open_signal"] = None
    save_state()

# ========= TTL / Anti-trava =========
async def expire_open_if_needed():
    osig = STATE.get("open_signal")
    if not osig: return
    if _open_age_secs() >= CLOSE_STUCK_AFTER_SEC:
        STATE["open_signal"] = None; save_state(); return
    exp = osig.get("expires_at")
    if exp and datetime.fromisoformat(exp) <= now_local():
        STATE["open_signal"] = None; save_state()
        await tg_send(TARGET_CHAT_ID, "⏳ Encerrado por timeout (TTL) — descartado")

# ========= Loops =========
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
                    if RULES_ENABLED and not STATE.get("open_signal") and STATE.get("rules_gale_stage",0)==0:
                        target_human = rules_should_fire(list(STATE["rules_recent3"]))
                        if target_human:
                            STATE["rules_target_side"] = human_to_side(target_human)
                            STATE["rules_gale_stage"]  = 0
                            await rules_try_open()
                save_state()
        except Exception as e:
            log.error("auto_loop: %s", e)
        await asyncio.sleep(AUTO_OPEN_INTERVAL_SEC)

async def heartbeat_loop():
    while True:
        try:
            await maybe_close_by_site()
            await expire_open_if_needed()
        except Exception as e:
            log.error("hb: %s", e)
        await asyncio.sleep(HEARTBEAT_SEC)

@app.on_event("startup")
async def _start():
    asyncio.create_task(auto_loop())
    asyncio.create_task(heartbeat_loop())

# ========= Rotas =========
@app.get("/")
async def root():
    return {"ok": True, "service":"TipMiner 100% • Regras(3) • Gales • Cooldown • Heartbeat", "tz": TZ_NAME}

@app.get("/debug/tipminer")
async def dbg_tm():
    try:
        sig, side = await tipminer_snapshot()
        return {"ok": True, "signature": sig, "last_side": side, "recent3": list(STATE["rules_recent3"])}
    except Exception as e:
        return {"ok": False, "err": str(e)}

@app.get("/debug/state")
async def dbg_state():
    return {
        "open_signal": STATE.get("open_signal"),
        "gales": STATE.get("rules_gale_stage"),
        "target": STATE.get("rules_target_side"),
        "cooldown_rounds": STATE.get("rules_cooldown_rounds"),
        "totals": STATE.get("totals"),
        "recent3": list(STATE["rules_recent3"]),
    }

@app.get("/debug/reset-state")
async def dbg_reset():
    STATE["open_signal"]=None
    STATE["rules_target_side"]=None
    STATE["rules_gale_stage"]=0
    STATE["rules_cooldown_rounds"]=0
    STATE["rules_recent3"].clear()
    save_state()
    return {"ok": True}

@app.get("/debug/force-close")
async def dbg_force_close():
    osig = STATE.get("open_signal")
    if not osig: return {"ok": False, "err":"no open"}
    res = await _apply_external_close(osig["chosen_side"])
    if not res: return {"ok": False, "err":"no external color"}
    # aplica placar como no normal
    if res=="G":
        STATE["totals"]["greens"]+=1; STATE["streak_green"]+=1
        STATE["rules_target_side"]=None; STATE["rules_gale_stage"]=0
    else:
        STATE["totals"]["reds"]+=1; STATE["streak_green"]=0
    await announce_outcome(res, osig["chosen_side"])
    STATE["open_signal"]=None; save_state()
    return {"ok": True, "result": res}