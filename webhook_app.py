# -*- coding: utf-8 -*-
# Fantan — Número Seco (com webhook + retroativo automático via Telethon)

import os, re, json, time, logging, math, asyncio
from typing import Dict, Tuple, List, Optional
from collections import defaultdict, deque
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fantan-numero-seco")

# ========= ENV =========
BOT_TOKEN   = os.getenv("TG_BOT_TOKEN") or ""
if not BOT_TOKEN:
    raise RuntimeError("Faltando TG_BOT_TOKEN")

CHANNEL_ID  = int(os.getenv("CHANNEL_ID", "-1002810508717"))
PUBLIC_URL  = (os.getenv("PUBLIC_URL") or "").rstrip("/")
COOLDOWN_S  = int(os.getenv("COOLDOWN_S", "10"))

N_MAX        = int(os.getenv("N_MAX", "4"))
MIN_SUP_G0   = int(os.getenv("MIN_SUP_G0", "20"))
MIN_SUP_G1   = int(os.getenv("MIN_SUP_G1", "20"))
CONF_MIN     = float(os.getenv("CONF_MIN", "0.90"))
Z_WILSON     = float(os.getenv("Z_WILSON", "1.96"))

SEND_SIGNALS   = int(os.getenv("SEND_SIGNALS", "1"))
REPORT_EVERY   = int(os.getenv("REPORT_EVERY", "5"))
RESULTS_WINDOW = int(os.getenv("RESULTS_WINDOW", "30"))

# Telethon (retroativo)
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
API_ID  = int(os.getenv("API_ID", "0") or "0")
API_HASH= os.getenv("API_HASH", "").strip()
ENABLE_SCRAPE  = bool(SESSION_STRING and API_ID and API_HASH)

SCRAPE_EVERY_S = int(os.getenv("SCRAPE_EVERY_S", "300"))   # 5 min
SCRAPE_LIMIT   = int(os.getenv("SCRAPE_LIMIT", "400"))     # por rodada

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp  = Dispatcher(bot)
app = FastAPI()

# ========= Estado persistente =========
STATE_FILE = "data/state.json"
os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
state = {
    "cooldown_until": 0.0,
    "sinais_enviados": 0,
    "greens_total": 0,
    "reds_total": 0,
    "last_scraped_id": 0,
}
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            state.update(json.load(open(STATE_FILE, "r", encoding="utf-8")))
    except Exception as e:
        log.warning("Falha load_state: %s", e)
def save_state():
    try:
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Falha save_state: %s", e)
load_state()

# ========= Modelo n-gramas =========
pattern_counts_g0: Dict[Tuple[int,...], List[int]] = defaultdict(lambda: [0,0,0,0,0])
pattern_totals_g0: Dict[Tuple[int,...], int]       = defaultdict(int)
pattern_hits_g1:   Dict[Tuple[int,...], List[int]] = defaultdict(lambda: [0,0,0,0,0])
pattern_trials_g1: Dict[Tuple[int,...], int]       = defaultdict(int)

recent_nums = deque(maxlen=N_MAX)
hist_long  = deque(maxlen=300)
hist_short = deque(maxlen=60)

# ========= Parsers =========
re_sinal = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
re_seq   = re.compile(r"Sequ[eê]ncia[:\s]*([^\n]+)", re.I)
re_apos  = re.compile(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", re.I)
re_close = re.compile(r"APOSTA\s+ENCERRADA", re.I)
re_green = re.compile(r"\bGREEN\b|✅", re.I)
re_red   = re.compile(r"\bRED\b|❌", re.I)

def eh_sinal(txt:str)->bool: return bool(re_sinal.search(txt or ""))
def extrai_apos(txt:str)->Optional[int]:
    m = re_apos.search(txt or ""); return int(m.group(1)) if m else None
def extrai_sequencia(txt:str)->List[int]:
    m = re_seq.search(txt or "")
    if not m: return []
    return [int(x) for x in re.findall(r"[1-4]", m.group(1))]
def eh_resultado(txt:str)->Optional[int]:
    u = (txt or "").upper()
    if not re_close.search(u): return None
    if re_green.search(u): return 1
    if re_red.search(u):   return 0
    return None

# ========= Alimentação do modelo =========
def alimentar_sequencia(nums: List[int]):
    if not nums: return
    L = len(nums)
    for i in range(1, L):
        nxt = nums[i]
        if not (1 <= nxt <= 4): continue
        nxt2 = nums[i+1] if (i+1 < L and 1 <= nums[i+1] <= 4) else None
        for n in range(1, N_MAX+1):
            if i-n < 0: break
            pad = tuple(nums[i-n:i])
            if not pad: continue
            if any((x<1 or x>4) for x in pad): continue
            pattern_counts_g0[pad][nxt] += 1
            pattern_totals_g0[pad]      += 1
            if nxt2 is not None:
                pattern_trials_g1[pad] += 1
                pattern_hits_g1[pad][nxt] += 1
                if nxt2 != nxt:
                    pattern_hits_g1[pad][nxt2] += 1
    for x in nums:
        if 1 <= x <= 4:
            if len(recent_nums) == N_MAX: recent_nums.popleft()
            recent_nums.append(x)

# ========= Estatística =========
def wilson_lower(successes:int, n:int, z:float=1.96)->float:
    if n==0: return 0.0
    phat = successes / n
    denom = 1 + z*z/n
    centre = phat + z*z/(2*n)
    margin = z*math.sqrt((phat*(1-phat) + z*z/(4*n))/n)
    return max(0.0, (centre - margin)/denom)

def contexto_candidato(apos: Optional[int])->List[Tuple[int,...]]:
    base = list(recent_nums)
    if apos is not None:
        if not base or base[-1] != apos: base.append(apos)
    cands = []
    for n in range(min(N_MAX, len(base)), 0, -1):
        cands.append(tuple(base[-n:]))
    return cands

# ========= Decisão =========
def decidir_numero(apos: Optional[int]):
    cands = contexto_candidato(apos)
    for pad in cands:
        trials = pattern_trials_g1.get(pad, 0)
        if trials >= MIN_SUP_G1:
            hits = pattern_hits_g1.get(pad, [0,0,0,0,0])
            for x in (1,2,3,4):
                if hits[x] == trials and trials > 0:
                    return ("G1_100", x, pad, len(pad), 1.0, 1.0, trials, {})
    for pad in cands:
        total = pattern_totals_g0.get(pad, 0)
        if total < MIN_SUP_G0: continue
        cnts = pattern_counts_g0.get(pad, [0,0,0,0,0])
        for x in (1,2,3,4):
            s0 = cnts[x]
            wl = wilson_lower(s0, total, Z_WILSON)
            if wl >= CONF_MIN:
                return ("G0_90", x, pad, len(pad), wl, s0/total, total, {})
    return None

# ========= Scraper automático =========
SCRAPER_READY = False
telethon_client = None

async def scraper_boot():
    global telethon_client, SCRAPER_READY
    if not ENABLE_SCRAPE: return
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        telethon_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await telethon_client.connect()
        if not await telethon_client.is_user_authorized():
            log.error("SESSION_STRING inválida.")
            return
        SCRAPER_READY = True
        log.info("Scraper conectado.")
    except Exception as e:
        log.exception("Falha Telethon: %s", e)

async def scrape_once(limit: int = SCRAPE_LIMIT):
    if not SCRAPER_READY: return
    try:
        msgs = await telethon_client.get_messages(CHANNEL_ID, limit=limit)
        last_id = state.get("last_scraped_id", 0)
        for m in reversed(msgs):
            if not m or not m.id: continue
            if m.id <= last_id: continue
            txt = (m.message or "").strip()
            if not txt: continue
            seq = extrai_sequencia(txt)
            if seq: alimentar_sequencia(seq)
            r = eh_resultado(txt)
            if r is not None:
                if r == 1: state["greens_total"] += 1
                else: state["reds_total"] += 1
            state["last_scraped_id"] = m.id
        save_state()
    except Exception as e:
        log.error("Erro scrape: %s", e)

async def scraper_loop():
    if not ENABLE_SCRAPE: return
    while True:
        await scrape_once()
        await asyncio.sleep(SCRAPE_EVERY_S)

# ========= Webhook =========
@app.on_event("startup")
async def on_startup():
    if PUBLIC_URL:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(f"{PUBLIC_URL}/webhook/{BOT_TOKEN}")
    if ENABLE_SCRAPE:
        await scraper_boot()
        asyncio.create_task(scraper_loop())

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.body()
    update = types.Update(**json.loads(data.decode("utf-8")))
    await dp.process_update(update)
    return {"ok": True}

# ========= Endpoint manual (opcional) =========
class IngestItem(BaseModel):
    text: str

class IngestPayload(BaseModel):
    items: List[IngestItem]

@app.post("/ingest")
async def ingest(payload: IngestPayload):
    for it in payload.items:
        seq = extrai_sequencia(it.text)
        if seq: alimentar_sequencia(seq)
        r = eh_resultado(it.text)
        if r is not None:
            if r == 1: state["greens_total"] += 1
            else: state["reds_total"] += 1
    save_state()
    return {"ok": True}