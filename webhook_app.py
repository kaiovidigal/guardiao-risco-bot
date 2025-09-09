# -*- coding: utf-8 -*-
# Fantan — Webhook completo (FastAPI + Aiogram + Telethon)
# • Núcleo IA: n-gramas
# • Prioridade: Até G1 = 100% (amostra mínima)
# • Fallback:   G0 ≥ 90% (Wilson 95%, amostra mínima)
# • Sinal só quando ≥ 90% | Neutro caso contrário
# • Resumo automático a cada REPORT_EVERY sinais (ex: 5)
# • /ingest para treino por JSON (retroativo offline)
# • /scrape para retroativo online (canal público + SESSION_STRING)
# • Persistência simples (data/state.json)

import os, re, json, time, logging, math, asyncio
from typing import Dict, Tuple, List, Optional
from collections import defaultdict, deque
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, types

# ---------- Telethon (userbot p/ retroativo online)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import SessionPasswordNeededError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fantan-webhook")

# =========================
# ENV / Config
# =========================
BOT_TOKEN   = os.getenv("TG_BOT_TOKEN") or ""
if not BOT_TOKEN:
    raise RuntimeError("Faltando TG_BOT_TOKEN")

# Canal:
CHANNEL_ID   = int(os.getenv("CHANNEL_ID", "-1002810508717"))      # id numérico
CHANNEL_USER = (os.getenv("CHANNEL_USERNAME") or "").strip()       # username público, ex: "fantanvidigal" (sem @)

PUBLIC_URL  = (os.getenv("PUBLIC_URL") or "").rstrip("/")          # URL pública do Render
COOLDOWN_S  = int(os.getenv("COOLDOWN_S", "10"))

# Modelo / critérios
N_MAX        = int(os.getenv("N_MAX", "4"))
MIN_SUP_G0   = int(os.getenv("MIN_SUP_G0", "20"))
MIN_SUP_G1   = int(os.getenv("MIN_SUP_G1", "20"))
CONF_MIN     = float(os.getenv("CONF_MIN", "0.90"))
Z_WILSON     = float(os.getenv("Z_WILSON", "1.96"))

# Relatórios
SEND_SIGNALS   = int(os.getenv("SEND_SIGNALS", "1"))
REPORT_EVERY   = int(os.getenv("REPORT_EVERY", "5"))
RESULTS_WINDOW = int(os.getenv("RESULTS_WINDOW", "30"))

# Telethon (retroativo online)
API_ID         = int(os.getenv("API_ID", "0"))
API_HASH       = os.getenv("API_HASH", "") or ""
SESSION_STRING = os.getenv("SESSION_STRING", "")
ENABLE_SCRAPE  = bool(SESSION_STRING and API_ID and API_HASH)

# =========================
# Bots & App
# =========================
bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp  = Dispatcher(bot)
app = FastAPI()

# Telethon client (lazy)
tele_client: Optional[TelegramClient] = None

# =========================
# Persistência simples
# =========================
STATE_FILE = "data/state.json"
os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
state = {
    "cooldown_until": 0.0,
    "sinais_enviados": 0,
    "greens_total": 0,
    "reds_total": 0,
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

# =========================
# Buffers / Modelo n-gramas
# =========================
pattern_counts_g0: Dict[Tuple[int,...], List[int]] = defaultdict(lambda: [0,0,0,0,0])
pattern_totals_g0: Dict[Tuple[int,...], int]       = defaultdict(int)
pattern_hits_g1:   Dict[Tuple[int,...], List[int]] = defaultdict(lambda: [0,0,0,0,0])
pattern_trials_g1: Dict[Tuple[int,...], int]       = defaultdict(int)

recent_nums = deque(maxlen=N_MAX)
hist_long   = deque(maxlen=300)
hist_short  = deque(maxlen=60)

# =========================
# Parsers
# =========================
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

# =========================
# Alimentação do modelo
# =========================
def alimentar_sequencia(nums: List[int]):
    """Para cada i, pad=nums[i-n:i]
       G0: conta nxt = nums[i]
       G1: se houver i+1, trial++ e hit_X++ para nxt e nxt2
    """
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

# =========================
# Estatística / Decisão
# =========================
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

def decidir_numero(apos: Optional[int]):
    """1) G1=100% (trials≥MIN_SUP_G1)
       2) G0 ≥ 90% (Wilson; total≥MIN_SUP_G0)
       Caso contrário: None (neutro)
    """
    cands = contexto_candidato(apos)

    # 1) Até G1 = 100%
    for pad in cands:
        trials = pattern_trials_g1.get(pad, 0)
        if trials >= MIN_SUP_G1:
            hits = pattern_hits_g1.get(pad, [0,0,0,0,0])
            best = None
            for x in (1,2,3,4):
                if hits[x] == trials and trials > 0:
                    item = (trials, -x, x)
                    if (best is None) or (item > best):
                        best = item
            if best:
                trials, _negx, x = best
                cnts_g0 = pattern_counts_g0.get(pad, [0,0,0,0,0])
                total_g0 = max(1, pattern_totals_g0.get(pad, 1))
                dist = {i: (cnts_g0[i]/total_g0) for i in (1,2,3,4)}
                return ("G1_100", x, pad, len(pad), 1.0, 1.0, trials, dist)

    # 2) G0 ≥ 90% (Wilson)
    for pad in cands:
        total = pattern_totals_g0.get(pad, 0)
        if total < MIN_SUP_G0:
            continue
        cnts = pattern_counts_g0.get(pad, [0,0,0,0,0])
        best = None
        for x in (1,2,3,4):
            s0 = cnts[x]
            wl = wilson_lower(s0, total, Z_WILSON)
            if wl >= CONF_MIN:
                p_hat = s0/total
                item = (wl, total, -x, x, p_hat)
                if (best is None) or (item > best):
                    best = item
        if best:
            wl, total, _negx, x, p_hat = best
            dist = {i: (cnts[i]/total) for i in (1,2,3,4)}
            return ("G0_90", x, pad, len(pad), wl, p_hat, total, dist)

    return None

# =========================
# Métricas / Mensagens
# =========================
def fmt_dist(dist: Dict[int,float])->str:
    return " | ".join(f"{i}:{dist[i]*100:.1f}%" for i in (1,2,3,4))

def taxa(d): 
    d = list(d); return (sum(d)/len(d)) if d else 0.0

def taxa_ultimos(d, n):
    d = list(d)[-n:]; return (sum(d)/len(d)) if d else 0.0

def resumo_lote_simples():
    lote = list(hist_long)[-REPORT_EVERY:]
    greens_lote = sum(lote)
    reds_lote   = len(lote) - greens_lote
    return greens_lote, reds_lote

async def enviar_sinal(dec):
    tipo, x, pad, n, score, p_hat, total, dist = dec
    greens_total = state.get("greens_total", 0)
    reds_total   = state.get("reds_total", 0)
    greens_lote, reds_lote = resumo_lote_simples()
    bloco_metrics = (
        f"\n\n📊 Parcial: Geral <b>{greens_total}✅ / {reds_total}❌</b> | "
        f"Últimos {REPORT_EVERY}: <b>{greens_lote}✅ / {reds_lote}❌</b>"
    )
    if tipo == "G1_100":
        msg = (
            "🟢 <b>SINAL (NÚMERO SECO)</b>\n"
            f"🎯 Número: <b>{x}</b>\n"
            f"📍 Padrão: <b>{pad}</b> | N={n}\n"
            f"🔒 Até G1: <b>100%</b> (amostra={total})\n"
            f"📈 Distribuição G0: {fmt_dist(dist)}"
            + bloco_metrics
        )
    else:
        msg = (
            "🟢 <b>SINAL (NÚMERO SECO)</b>\n"
            f"🎯 Número: <b>{x}</b>\n"
            f"📍 Padrão: <b>{pad}</b> | N={n}\n"
            f"🧠 G0 ≥ 90% (Wilson 95%: <b>{score*100:.1f}%</b> | p̂={p_hat*100:.1f}% | amostra={total})\n"
            f"📈 Distribuição G0: {fmt_dist(dist)}"
            + bloco_metrics
        )
    await bot.send_message(CHANNEL_ID, msg, parse_mode="HTML")

async def enviar_neutro(apos_ctx: Optional[Tuple[int,...]]):
    msg = (
        "😐 <b>Neutro — avaliando combinações</b>\n"
        f"📍 Contexto atual: <b>{apos_ctx if apos_ctx else '—'}</b>\n"
        f"🔎 Nenhum padrão com G1=100% (N≥{MIN_SUP_G1}) ou G0≥90% (Wilson, N≥{MIN_SUP_G0})."
    )
    await bot.send_message(CHANNEL_ID, msg, parse_mode="HTML")

async def envia_resumo_periodico():
    short_wr = taxa(hist_short)
    long_wr  = taxa(hist_long)
    lastN_wr = taxa_ultimos(hist_long, RESULTS_WINDOW)
    total    = state.get("greens_total",0) + state.get("reds_total",0)
    overall  = (state.get("greens_total",0)/total) if total else 0.0
    greens_lote, reds_lote = resumo_lote_simples()
    msg = (
        f"📊 <b>Resumo ({REPORT_EVERY} sinais)</b>\n"
        f"🧪 Lote atual ({REPORT_EVERY}): <b>{greens_lote}✅ / {reds_lote}❌</b>\n"
        f"🌐 Geral: <b>{state.get('greens_total',0)}✅ / {state.get('reds_total',0)}❌</b> "
        f"({overall*100:.1f}%)\n\n"
        f"⏱️ Últimos {RESULTS_WINDOW}: <b>{lastN_wr*100:.1f}%</b>\n"
        f"📚 Curto (≈{len(hist_short)}): <b>{short_wr*100:.1f}%</b>\n"
        f"📖 Longo (≈{len(hist_long)}): <b>{long_wr*100:.1f}%</b>"
    )
    await bot.send_message(CHANNEL_ID, msg, parse_mode="HTML")

# =========================
# Telegram Bot Handlers
# =========================
@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    info = (
        "🤖 Fantan — Número Seco por combinações\n"
        f"• Canal: <code>{CHANNEL_ID}</code> {'(@'+CHANNEL_USER+')' if CHANNEL_USER else ''}\n"
        f"• N_MAX={N_MAX} | MIN_SUP_G0={MIN_SUP_G0} | MIN_SUP_G1={MIN_SUP_G1} | CONF_MIN={CONF_MIN}\n"
        f"• REPORT_EVERY={REPORT_EVERY} | RESULTS_WINDOW={RESULTS_WINDOW}\n"
        "• Critérios: prioridade ‘Até G1=100%’, senão ‘G0≥90% (Wilson)’. "
        "Sinal só quando ≥ 90%; do contrário envia Neutro.\n"
        "• Entradas aceitas: ‘Sequência: ...’, ‘ENTRADA CONFIRMADA ...’, ‘APOSTA ENCERRADA ✅/❌’."
    )
    await msg.answer(info, parse_mode="HTML")

@dp.message_handler(commands=["status"])
async def cmd_status(msg: types.Message):
    await envia_resumo_periodico()

@dp.channel_post_handler(content_types=["text"])
async def on_channel_post(message: types.Message):
    if message.chat.id != CHANNEL_ID:
        return
    txt = (message.text or "").strip()
    if not txt:
        return

    # 1) Treino a partir de "Sequência:"
    seq = extrai_sequencia(txt)
    if seq:
        alimentar_sequencia(seq)

    # 2) Green/Red p/ métricas
    r = eh_resultado(txt)
    if r is not None:
        hist_long.append(r); hist_short.append(r)
        if r == 1: state["greens_total"] += 1
        else:      state["reds_total"]   += 1
        save_state()
        return

    # 3) SINAL
    if eh_sinal(txt):
        now = time.time()
        if now < state.get("cooldown_until", 0):
            return
        apos = extrai_apos(txt)
        dec = decidir_numero(apos)
        if dec is None:
            ctx = contexto_candidato(apos)
            await enviar_neutro(ctx[0] if ctx else None)
        else:
            if SEND_SIGNALS == 1:
                await enviar_sinal(dec)
                state["sinais_enviados"] += 1
                if REPORT_EVERY > 0 and state["sinais_enviados"] % REPORT_EVERY == 0:
                    await envia_resumo_periodico()
        state["cooldown_until"] = now + COOLDOWN_S
        save_state()

# =========================
# FastAPI: /ingest e /scrape
# =========================
class IngestItem(BaseModel):
    id: Optional[int] = None
    date: Optional[str] = None
    text: str

class IngestPayload(BaseModel):
    items: List[IngestItem]

@app.post("/ingest")
async def ingest(payload: IngestPayload):
    added_seq = 0; added_res = 0
    for it in payload.items:
        t = (it.text or "").strip()
        if not t: continue
        seq = extrai_sequencia(t)
        if seq:
            alimentar_sequencia(seq); added_seq += 1
        r = eh_resultado(t)
        if r is not None:
            hist_long.append(r); hist_short.append(r)
            if r == 1: state["greens_total"] += 1
            else:      state["reds_total"]   += 1
            added_res += 1
    save_state()
    return {"ok": True, "added_sequences": added_seq, "added_results": added_res}

async def ensure_tele_client() -> TelegramClient:
    global tele_client
    if tele_client: 
        return tele_client
    if not ENABLE_SCRAPE:
        raise RuntimeError("Defina SESSION_STRING + API_ID + API_HASH para habilitar /scrape.")
    tele_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await tele_client.connect()
    if not await tele_client.is_user_authorized():
        raise RuntimeError("SESSION_STRING inválida ou sem autorização.")
    if CHANNEL_USER:
        try:
            await tele_client(JoinChannelRequest(CHANNEL_USER))
        except Exception:
            pass
    return tele_client

@app.post("/scrape")
async def scrape():
    if not ENABLE_SCRAPE:
        raise HTTPException(
            status_code=400,
            detail=("Retroativo online desabilitado: defina SESSION_STRING + API_ID + API_HASH. "
                    "Alternativas: /ingest com export do Telegram Desktop.")
        )
    try:
        client = await ensure_tele_client()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Falha ao iniciar userbot: {e}")

    added_seq = 0
    added_res = 0
    scanned   = 0
    target    = CHANNEL_USER if CHANNEL_USER else CHANNEL_ID

    async for msg in client.iter_messages(entity=target, limit=None):
        scanned += 1
        t = (msg.message or "").strip()
        if not t:
            continue
        seq = extrai_sequencia(t)
        if seq:
            alimentar_sequencia(seq)
            added_seq += 1
        r = eh_resultado(t)
        if r is not None:
            hist_long.append(r); hist_short.append(r)
            if r == 1: state["greens_total"] += 1
            else:      state["reds_total"]   += 1
            added_res += 1
        if scanned % 5000 == 0:
            await asyncio.sleep(0.01)

    save_state()
    return {
        "ok": True,
        "scanned": scanned,
        "added_sequences": added_seq,
        "added_results": added_res,
        "g0_patterns": len(pattern_totals_g0),
        "g1_patterns": len(pattern_trials_g1),
    }

# =========================
# Webhook Telegram
# =========================
@app.on_event("startup")
async def on_startup():
    if PUBLIC_URL:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(f"{PUBLIC_URL}/webhook/{BOT_TOKEN}")
        log.info("Webhook setado: %s/webhook/%s", PUBLIC_URL, BOT_TOKEN)
    else:
        log.warning("PUBLIC_URL não definida; configure no Render.")

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.body()
    update = types.Update(**json.loads(data.decode("utf-8")))
    await dp.process_update(update)
    return {"ok": True}