# -*- coding: utf-8 -*-
# Fantan — Número Seco com padrões (n-grama):
# 1) Prioriza "Até G1 = 100%" (exato, com amostra mínima)
# 2) Fallback "G0 ≥ 90%" (limite inferior de Wilson 95% com amostra mínima)
# 3) Se nada bater, "Neutro — avaliando combinações"
# Extras:
# • /ingest para importar histórico retroativo (JSONL/JSON em lotes)
# • /scrape (com token) usando Telethon (userbot) para baixar o histórico e alimentar
# • Resumo automático a cada 5 sinais e métricas embutidas em cada SINAL
# • Suporte "Entrar após o k" no contexto

import os, re, json, time, logging, math, asyncio
from typing import Dict, Tuple, List, Optional
from collections import defaultdict, deque
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types

# ---- opcional: Telethon userbot embutido
ENABLE_USERBOT = int(os.getenv("ENABLE_USERBOT", "1"))
try:
    from telethon import TelegramClient
except Exception:
    TelegramClient = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fantan-numero-seco")

# =========================
# ENV / Config
# =========================
BOT_TOKEN   = os.getenv("TG_BOT_TOKEN") or ""
if not BOT_TOKEN:
    raise RuntimeError("Faltando TG_BOT_TOKEN")

# Seu canal:
CHANNEL_ID  = int(os.getenv("CHANNEL_ID", "-1002810508717"))  # <- seu canal
PUBLIC_URL  = (os.getenv("PUBLIC_URL") or "").rstrip("/")
COOLDOWN_S  = int(os.getenv("COOLDOWN_S", "10"))

# Padrões / critérios
N_MAX        = int(os.getenv("N_MAX", "4"))
MIN_SUP_G0   = int(os.getenv("MIN_SUP_G0", "20"))
MIN_SUP_G1   = int(os.getenv("MIN_SUP_G1", "20"))
CONF_MIN     = float(os.getenv("CONF_MIN", "0.90"))
Z_WILSON     = float(os.getenv("Z_WILSON", "1.96"))

# Relatório
SEND_SIGNALS   = int(os.getenv("SEND_SIGNALS", "1"))
REPORT_EVERY   = int(os.getenv("REPORT_EVERY", "5"))
RESULTS_WINDOW = int(os.getenv("RESULTS_WINDOW", "30"))

# Userbot (Telethon)
API_ID        = int(os.getenv("API_ID", "0"))
API_HASH      = os.getenv("API_HASH", "")
EXPORT_CHAN   = os.getenv("EXPORT_CHANNEL", "")     # @canal ou link privado
SCRAPER_TOKEN = os.getenv("SCRAPER_TOKEN", "")      # senha do endpoint /scrape
SCRAPER_FILTER= os.getenv("SCRAPER_FILTER_REGEX", r"Sequ[eê]ncia|ENTRADA\s+CONFIRMADA|APOSTA\s+ENCERRADA")
SCRAPER_SINCE = os.getenv("SCRAPER_SINCE", "")      # YYYY-MM-DD
SCRAPER_UNTIL = os.getenv("SCRAPER_UNTIL", "")      # YYYY-MM-DD
SCRAPER_LIMIT = int(os.getenv("SCRAPER_LIMIT", "0"))# 0 = tudo
SCRAPER_ON_START = int(os.getenv("SCRAPER_ON_START", "0"))

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp  = Dispatcher(bot)
app = FastAPI()

# =========================
# Estado persistente
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
# Modelo (n-gramas)
# =========================
# G0: próximo número
pattern_counts_g0: Dict[Tuple[int,...], List[int]] = defaultdict(lambda: [0,0,0,0,0])  # idx 1..4
pattern_totals_g0: Dict[Tuple[int,...], int]       = defaultdict(int)
# G1: próximo OU o seguinte (exato)
pattern_hits_g1:   Dict[Tuple[int,...], List[int]] = defaultdict(lambda: [0,0,0,0,0])
pattern_trials_g1: Dict[Tuple[int,...], int]       = defaultdict(int)

recent_nums = deque(maxlen=N_MAX)

# Histórico de resultados (para resumo)
hist_long  = deque(maxlen=300)
hist_short = deque(maxlen=60)

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
# Alimentação do modelo (inclui G1 exato)
# =========================
def alimentar_sequencia(nums: List[int]):
    """Para cada i, pad = nums[i-n:i] (n=1..N_MAX)
       G0: conta nxt = nums[i]
       G1: se houver i+1, trial++ e hit_X++ se nxt==X ou nxt2==X
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

            # G0
            pattern_counts_g0[pad][nxt] += 1
            pattern_totals_g0[pad]      += 1

            # G1 (precisa t+1)
            if nxt2 is not None:
                pattern_trials_g1[pad] += 1
                pattern_hits_g1[pad][nxt] += 1
                if nxt2 != nxt:
                    pattern_hits_g1[pad][nxt2] += 1

    # contexto online
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
    """Prioridade: G1=100% (trials>=MIN_SUP_G1) -> G0≥90% (Wilson; total>=MIN_SUP_G0)."""
    cands = contexto_candidato(apos)

    # 1) Até G1 = 100%
    for pad in cands:
        trials = pattern_trials_g1.get(pad, 0)
        if trials >= MIN_SUP_G1:
            hits = pattern_hits_g1.get(pad, [0,0,0,0,0])
            best = None
            for x in (1,2,3,4):
                if hits[x] == trials and trials > 0:
                    # desempate por trials (mais suporte), depois menor X
                    item = (trials, -x, x)
                    if (best is None) or (item > best):
                        best = item
            if best:
                trials, _negx, x = best
                # usar dist G0 como info de distribuição
                cnts_g0 = pattern_counts_g0.get(pad, [0,0,0,0,0])
                total_g0 = pattern_totals_g0.get(pad, 1) or 1
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
# Mensagens / Métricas
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
# Handlers Telegram
# =========================
@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    info = (
        "🤖 Fantan — Número Seco por combinações\n"
        f"• Canal: <code>{CHANNEL_ID}</code>\n"
        f"• N_MAX={N_MAX} | MIN_SUP_G0={MIN_SUP_G0} | MIN_SUP_G1={MIN_SUP_G1} | CONF_MIN={CONF_MIN}\n"
        f"• REPORT_EVERY={REPORT_EVERY} | RESULTS_WINDOW={RESULTS_WINDOW}\n"
        "• Critérios: prioridade ‘Até G1=100%’, senão ‘G0≥90% (Wilson)’.\n"
        "• Entradas: ‘Sequência: ...’, ‘ENTRADA CONFIRMADA ...’, ‘APOSTA ENCERRADA ✅/❌’."
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
    if not txt: return

    # 1) Treino com SEQUÊNCIA
    seq = extrai_sequencia(txt)
    if seq: alimentar_sequencia(seq)

    # 2) GREEN/RED para métricas
    r = eh_resultado(txt)
    if r is not None:
        hist_long.append(r); hist_short.append(r)
        if r == 1: state["greens_total"] += 1
        else: state["reds_total"] += 1
        save_state()
        return

    # 3) SINAL → decidir
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
# FastAPI: ingest/scrape/webhook
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
            else: state["reds_total"] += 1
            added_res += 1
    save_state()
    return {"ok": True, "added_sequences": added_seq, "added_results": added_res}

def _parse_date(d):
    return datetime.strptime(d, "%Y-%m-%d") if d else None

@app.post("/scrape")
async def scrape(request: Request):
    # proteção simples por token
    token = (request.query_params.get("token") or "").strip()
    if not SCRAPER_TOKEN or token != SCRAPER_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not ENABLE_USERBOT or TelegramClient is None:
        raise HTTPException(status_code=400, detail="Userbot desabilitado ou Telethon ausente")
    if not (API_ID and API_HASH and EXPORT_CHAN):
        raise HTTPException(status_code=400, detail="Defina API_ID, API_HASH e EXPORT_CHANNEL")

    since_dt = _parse_date(SCRAPER_SINCE)
    until_dt = _parse_date(SCRAPER_UNTIL)
    rx = re.compile(SCRAPER_FILTER, re.I) if SCRAPER_FILTER else None

    async def _run():
        client = TelegramClient("sessao_scrape", API_ID, API_HASH)
        await client.start()
        ent = await client.get_entity(EXPORT_CHAN)
        n = 0
        async for m in client.iter_messages(ent, reverse=True, limit=None if SCRAPER_LIMIT==0 else SCRAPER_LIMIT):
            if m.date:
                if since_dt and m.date < since_dt: continue
                if until_dt and m.date > until_dt: continue
            text = m.message or ""
            if not text: continue
            if rx and not rx.search(text): continue
            # alimentar diretamente
            seq = extrai_sequencia(text)
            if seq:
                alimentar_sequencia(seq); n += 1
            r = eh_resultado(text)
            if r is not None:
                hist_long.append(r); hist_short.append(r)
                if r == 1: state["greens_total"] += 1
                else: state["reds_total"] += 1
        save_state()
        await client.disconnect()
        return n

    n = await _run()
    return {"ok": True, "sequences_ingested": n}

@app.on_event("startup")
async def on_startup():
    if PUBLIC_URL:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(f"{PUBLIC_URL}/webhook/{BOT_TOKEN}")
    else:
        log.warning("PUBLIC_URL não definida; configure no Render.")

    # scraper automático se habilitado
    if SCRAPER_ON_START and ENABLE_USERBOT and TelegramClient and API_ID and API_HASH and EXPORT_CHAN and SCRAPER_TOKEN:
        # roda em background
        async def kickoff():
            from starlette.concurrency import run_in_threadpool
            try:
                class DummyReq:  # simula chamada local ao /scrape
                    query_params = {"token": SCRAPER_TOKEN}
                await scrape(DummyReq)
            except Exception as e:
                log.warning("SCRAPER_ON_START falhou: %s", e)
        asyncio.create_task(kickoff())

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.body()
    update = types.Update(**json.loads(data.decode("utf-8")))
    await dp.process_update(update)
    return {"ok": True}