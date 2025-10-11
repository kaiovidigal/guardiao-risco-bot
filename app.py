import os, re, json, asyncio, pytz, logging, hashlib
from datetime import datetime, timedelta
from collections import deque
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import httpx

# ====== ENVs ======
TG_BOT_TOKEN   = os.environ["TG_BOT_TOKEN"]
TARGET_CHAT_ID = int(os.environ["TARGET_CHAT_ID"])
TZ_NAME        = os.getenv("TZ","UTC")
LOCAL_TZ       = pytz.timezone(TZ_NAME)

TIPMINER_URL   = os.getenv("TIPMINER_URL","https://www.tipminer.com/br/historico/jonbet/bac-bo")
FALLBACK_URL   = os.getenv("FALLBACK_URL","https://casinoscores.com/pt-br/bac-bo/")
REQUIRE_EXTERNAL_CONFIRM = os.getenv("REQUIRE_EXTERNAL_CONFIRM","1")=="1"
GREEN_RULE     = os.getenv("GREEN_RULE","opposite").lower()  # opposite|follow
HEARTBEAT_SEC  = int(os.getenv("HEARTBEAT_SEC","3"))
AUTO_OPEN_INTERVAL_SEC = int(os.getenv("AUTO_OPEN_INTERVAL_SEC","2"))
RULES_ENABLED  = os.getenv("RULES_ENABLED","1")=="1"
RULES_MAX_GALES= int(os.getenv("RULES_MAX_GALES","2"))
RULES_COOLDOWN_ROUNDS=int(os.getenv("RULES_COOLDOWN_ROUNDS","3"))
OPEN_TTL_SEC   = int(os.getenv("OPEN_TTL_SEC","90"))
MIN_RESULT_DELAY_SEC = int(os.getenv("MIN_RESULT_DELAY_SEC","8"))
STATE_PATH     = os.getenv("STATE_PATH","./state/state.json")
API            = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ====== App / log ======
app = FastAPI()
log = logging.getLogger("uvicorn.error")
def now(): return datetime.now(LOCAL_TZ)

# ====== State ======
STATE = {
    "open_signal": None,
    "last_publish_ts": None,
    "totals": {"greens":0,"reds":0},
    "streak_green": 0,
    "auto_last_round_sig": None,
    "rules_recent3": deque(maxlen=3),
    "rules_target_side": None,
    "rules_gale_stage": 0,
    "rules_cooldown_rounds": 0,
}
def _ensure_dir(p):
    d=os.path.dirname(p); 
    if d and not os.path.exists(d): os.makedirs(d,exist_ok=True)
def _jsonable(o):
    if isinstance(o,deque): return list(o)
    if isinstance(o,dict): return {k:_jsonable(v) for k,v in o.items()}
    if isinstance(o,list): return [_jsonable(x) for x in o]
    return o
def save_state():
    _ensure_dir(STATE_PATH)
    tmp=STATE_PATH+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(_jsonable(STATE),f,ensure_ascii=False)
    os.replace(tmp,STATE_PATH)
def load_state():
    global STATE
    try:
        with open(STATE_PATH,"r",encoding="utf-8") as f: data=json.load(f)
        STATE.update(data); STATE["rules_recent3"]=deque(data.get("rules_recent3",[]),maxlen=3)
    except Exception: save_state()
load_state()

async def tg_send(text:str):
    try:
        async with httpx.AsyncClient(timeout=20) as cli:
            await cli.post(f"{API}/sendMessage", json={
                "chat_id": TARGET_CHAT_ID, "text": text, "parse_mode":"HTML", "disable_web_page_preview": True
            })
    except Exception as e:
        log.error(f"tg_send error: {e}")

# ====== TipMiner fetch (anti-cache) ======
TM_PLAYER_RE = re.compile(r"(🔵|🟦|Player|Azul)", re.I)
TM_BANKER_RE = re.compile(r"(🔴|🟥|Banker|Vermelho)", re.I)
CS_PLAYER_RE = re.compile(r"(Jogador|Player)", re.I)
CS_BANKER_RE = re.compile(r"(Banqueiro|Banker)", re.I)

async def _fetch(url:str)->str|None:
    ts=int(now().timestamp()); sep="&" if "?" in url else "?"
    u=f"{url}{sep}_t={ts}"
    try:
        async with httpx.AsyncClient(timeout=15, headers={
            "User-Agent":"Mozilla/5.0", "Cache-Control":"no-cache"
        }) as cli:
            r=await cli.get(u)
            if r.status_code==200 and r.text: return r.text
    except Exception as e:
        log.warning(f"fetch fail {url}: {e}")
    return None

def _pick_side(html:str, re_p, re_b)->str|None:
    if not html: return None
    head=html[:20000]
    mp=re_p.search(head); mb=re_b.search(head)
    if mp and not mb: return "player"
    if mb and not mp: return "banker"
    if mp and mb: return "player" if mp.start()<mb.start() else "banker"
    return None

def _signature(html:str)->str|None:
    if not html: return None
    return hashlib.sha1(html[:6000].encode("utf-8","ignore")).hexdigest()

async def tipminer_snapshot():
    html = await _fetch(TIPMINER_URL)
    sig  = _signature(html or "")
    side = _pick_side(html or "", TM_PLAYER_RE, TM_BANKER_RE)
    if not side:
        fb = await _fetch(FALLBACK_URL)
        if fb:
            side = _pick_side(fb, CS_PLAYER_RE, CS_BANKER_RE)
            sig  = sig or _signature(fb)
    return sig, side

def side_to_human(s): return "Azul" if s=="player" else ("Vermelho" if s=="banker" else "Tie")
def human_to_side(h):
    h=(h or "").lower()
    if h in ("azul","player","p"): return "player"
    if h in ("vermelho","banker","b"): return "banker"
    if h in ("tie","empate"): return "empate"
    return None

def rules_fire(last3):
    if len(last3)<3: return None
    a=last3
    if a==['Vermelho','Vermelho','Vermelho']: return 'Azul'
    if a==['Azul','Azul','Azul']:             return 'Vermelho'
    if a==['Tie','Vermelho','Tie']:           return 'Azul'
    if a==['Tie','Azul','Tie']:               return 'Vermelho'
    if a==['Vermelho','Tie','Tie']:           return 'Azul'
    if a==['Azul','Tie','Tie']:               return 'Vermelho'
    if a==['Vermelho','Tie','Vermelho']:      return 'Azul'
    if a==['Azul','Tie','Azul']:              return 'Vermelho'
    return None

async def publish_entry(chosen:str):
    await tg_send(f"🚀 <b>ENTRADA AUTÔNOMA</b>\n{'🔵 Player' if chosen=='player' else '🔴 Banker'}\nOrigem: TipMiner + Regras")
    STATE["open_signal"] = {
        "chosen_side": chosen,
        "opened_epoch": int(now().timestamp()),
        "open_signature": _signature((await _fetch(TIPMINER_URL)) or ""),
        "expires_at": (now()+timedelta(seconds=OPEN_TTL_SEC)).isoformat()
    }
    STATE["last_publish_ts"]=now().isoformat()
    save_state()

def result_from(chosen, real):
    if not chosen or not real: return "R"
    if GREEN_RULE=="follow":  return "G" if chosen==real else "R"
    return "G" if chosen!=real else "R"

async def external_close():
    osig=STATE.get("open_signal"); if not osig: return
    # mínimo delay
    if int(now().timestamp()) - osig.get("opened_epoch",0) < MIN_RESULT_DELAY_SEC: return
    # ver cor real
    html = await _fetch(TIPMINER_URL)
    real = _pick_side(html or "", TM_PLAYER_RE, TM_BANKER_RE)
    if not real:
        fb = await _fetch(FALLBACK_URL)
        real = _pick_side(fb or "", CS_PLAYER_RE, CS_BANKER_RE)
    if not real and REQUIRE_EXTERNAL_CONFIRM: return
    res = result_from(osig["chosen_side"], real or "banker")
    if res=="G":
        STATE["totals"]["greens"]+=1; STATE["streak_green"]+=1
        STATE["rules_target_side"]=None; STATE["rules_gale_stage"]=0
    else:
        STATE["totals"]["reds"]+=1; STATE["streak_green"]=0
        if RULES_ENABLED and STATE.get("rules_target_side") and STATE["rules_gale_stage"]<RULES_MAX_GALES:
            STATE["rules_gale_stage"]+=1
            await publish_entry(STATE["rules_target_side"])
        else:
            STATE["rules_target_side"]=None; STATE["rules_gale_stage"]=0
            STATE["rules_cooldown_rounds"]=RULES_COOLDOWN_ROUNDS
    await tg_send(("🟩🟩🟩 <b>GREEN</b> 🟩🟩🟩" if res=="G" else "🟥🟥🟥 <b>LOSS</b> 🟥🟥🟥")
                  + f"\n⏱ {now().strftime('%H:%M:%S')}")
    STATE["open_signal"]=None; save_state()

async def auto_loop():
    while True:
        try:
            sig, side = await tipminer_snapshot()
            if sig and sig != STATE.get("auto_last_round_sig"):
                STATE["auto_last_round_sig"]=sig
                if side: STATE["rules_recent3"].append(side_to_human(side))
                if STATE["rules_cooldown_rounds"]>0:
                    STATE["rules_cooldown_rounds"]-=1
                elif RULES_ENABLED and not STATE.get("open_signal") and STATE.get("rules_gale_stage",0)==0:
                    target_h = rules_fire(list(STATE["rules_recent3"]))
                    if target_h:
                        STATE["rules_target_side"]=human_to_side(target_h)
                        STATE["rules_gale_stage"]=0
                        await publish_entry(STATE["rules_target_side"])
                save_state()
        except Exception as e:
            log.error(f"auto_loop: {e}")
        await asyncio.sleep(AUTO_OPEN_INTERVAL_SEC)

async def hb_loop():
    while True:
        try:
            await external_close()
            # TTL
            osig=STATE.get("open_signal")
            if osig and datetime.fromisoformat(osig["expires_at"])<=now():
                STATE["open_signal"]=None; save_state()
        except Exception as e:
            log.error(f"hb_loop: {e}")
        await asyncio.sleep(HEARTBEAT_SEC)

@app.on_event("startup")
async def _start():
    asyncio.create_task(auto_loop())
    asyncio.create_task(hb_loop())

# ====== Rotas ======
@app.get("/")
async def root(): return {"ok":True,"service":"TipMiner only"}
@app.get("/debug/tipminer")
async def dbg_tm():
    sig, side = await tipminer_snapshot()
    return {"ok":True,"signature":sig,"last_side":side}
@app.get("/debug/state")
async def dbg_state():
    s=dict(STATE); s["rules_recent3"]=list(STATE["rules_recent3"]); return s
@app.get("/debug/reset-state")
async def dbg_reset():
    global STATE
    STATE.update({
        "open_signal":None,"rules_recent3":deque(maxlen=3),
        "rules_target_side":None,"rules_gale_stage":0,"rules_cooldown_rounds":0
    }); save_state(); return {"ok":True}
@app.get("/debug/force-close")
async def dbg_force():
    if not STATE.get("open_signal"): return JSONResponse({"ok":False,"err":"no open_signal"},status_code=400)
    await external_close(); return {"ok":True}