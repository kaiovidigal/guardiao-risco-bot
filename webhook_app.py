# -*- coding: utf-8 -*-
# webhook_app.py — Fantan: Número Seco com Padrões (n-grama) e Confiança >= 90% (Wilson 95%)

import os, re, json, time, logging, math
from collections import defaultdict, deque
from typing import Dict, Tuple, List, Optional

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types

# =========================
# Config & Logging
# =========================
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("fantan-numero-seco")

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Faltando TG_BOT_TOKEN")

# Canal de destino (onde o bot posta) — coloque o ID numérico do seu canal
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1002810508717"))

# URL pública do Render (ou outro provedor) — ex.: https://seu-app.onrender.com
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")

# Anti-flood entre sinais
COOLDOWN_S = int(os.getenv("COOLDOWN_S", "10"))

# Parâmetros da mineração / decisão
N_MAX       = int(os.getenv("N_MAX", "4"))         # comprimento máximo do padrão (1..4)
MIN_SUP     = int(os.getenv("MIN_SUP", "20"))      # suporte mínimo do padrão
CONF_MIN    = float(os.getenv("CONF_MIN", "0.90")) # limiar de envio (Wilson lower 95%)
Z_WILSON    = float(os.getenv("Z_WILSON", "1.96")) # z=1.96 ≈ 95%

# Relatórios
SEND_SIGNALS  = int(os.getenv("SEND_SIGNALS", "1"))   # 1 envia sinal; 0 só imprime “Prévia/Neutro”
REPORT_EVERY  = int(os.getenv("REPORT_EVERY", "5"))   # a cada N sinais enviados
RESULTS_WIN   = int(os.getenv("RESULTS_WINDOW", "30"))

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp  = Dispatcher(bot)

# =========================
# Persistência simples
# =========================
STATE_FILE = "data/state.json"
os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
state = {
    "cooldown_until": 0.0,
    "sinais_enviados": 0,
    "greens_total": 0,
    "reds_total": 0
}

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            state.update(json.load(open(STATE_FILE, "r", encoding="utf-8")))
    except Exception as e:
        log.warning("Falha ao carregar state: %s", e)

def save_state():
    try:
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Falha ao salvar state: %s", e)

load_state()

# =========================
# Modelo e estatísticas
# =========================
# Mapa: pattern(tuple de ints de 1..4 com tamanho 1..N_MAX) -> contagens dos próximos números (1..4)
# Ex.: pad=(2,4,1) => counts = [0, c1, c2, c3, c4], total = sum(c1..c4)
pattern_counts: Dict[Tuple[int, ...], List[int]] = defaultdict(lambda: [0,0,0,0,0])
pattern_totals: Dict[Tuple[int, ...], int] = defaultdict(int)

# Para contexto online (últimos números observados)
recent_nums = deque(maxlen=N_MAX)

# Para relatórios GREEN/RED (se o canal publicar resultado)
hist_long  = deque(maxlen=300)
hist_short = deque(maxlen=60)

# =========================
# Regex dos parsers
# =========================
re_sinal   = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
re_seq     = re.compile(r"Sequ[eê]ncia[:\s]*([^\n]+)", re.I)
re_apos    = re.compile(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", re.I)
re_close   = re.compile(r"APOSTA\s+ENCERRADA", re.I)
re_green   = re.compile(r"\bGREEN\b|✅", re.I)
re_red     = re.compile(r"\bRED\b|❌", re.I)

def eh_sinal(txt: str) -> bool:
    return bool(re_sinal.search(txt or ""))

def extrai_apos(txt: str) -> Optional[int]:
    m = re_apos.search(txt or ""); 
    return int(m.group(1)) if m else None

def extrai_sequencia(txt: str) -> List[int]:
    m = re_seq.search(txt or "")
    if not m: return []
    return [int(x) for x in re.findall(r"[1-4]", m.group(1))]

def eh_resultado(txt: str) -> Optional[int]:
    u = (txt or "").upper()
    if not re_close.search(u): return None
    if re_green.search(u): return 1
    if re_red.search(u):   return 0
    return None

# =========================
# Atualização do modelo
# =========================
def alimentar_sequencia(nums: List[int]):
    """Alimenta a mineração de padrões com uma sequência crua (… a, b, c, …)."""
    if not nums: 
        return
    # Atualiza padrões n-grama
    # Para cada posição i, olhe para trás até N_MAX e conte (padrão -> próximo)
    for i in range(1, len(nums)):
        nxt = nums[i]
        if nxt < 1 or nxt > 4: 
            continue
        # constrói janelas de tamanho 1..N_MAX que terminam em i-1
        for n in range(1, N_MAX + 1):
            if i - n < 0: break
            pad = tuple(nums[i - n:i])   # últimos n números antes de nxt
            if not pad: 
                continue
            if any((x < 1 or x > 4) for x in pad):
                continue
            # conta
            pattern_counts[pad][nxt] += 1
            pattern_totals[pad]      += 1

    # Atualiza contexto online
    for x in nums:
        if 1 <= x <= 4:
            if len(recent_nums) == N_MAX:
                recent_nums.popleft()
            recent_nums.append(x)

# =========================
# Estatística: Limite inferior de Wilson (95% por padrão)
# =========================
def wilson_lower(successes: int, n: int, z: float = Z_WILSON) -> float:
    if n == 0:
        return 0.0
    phat = successes / n
    denom = 1 + z*z/n
    centre = phat + z*z/(2*n)
    margin = z*math.sqrt((phat*(1 - phat) + z*z/(4*n)) / n)
    return max(0.0, (centre - margin) / denom)

# =========================
# Decisão em tempo real
# =========================
def contexto_candidato(apos: Optional[int]) -> List[Tuple[int, ...]]:
    """
    Retorna lista de padrões candidatos (do maior N para o menor),
    respeitando 'Entrar após o k' se fornecido.
    """
    candidates = []
    # usamos os últimos números observados e/ou o 'apos'
    base = list(recent_nums)
    # se veio "após k", garantimos que o último do padrão é k:
    if apos is not None:
        base = (base + [apos]) if (not base or base[-1] != apos) else base

    # gerar padrões do maior para o menor
    for n in range(min(N_MAX, len(base)), 0, -1):
        pad = tuple(base[-n:])
        candidates.append(pad)
    return candidates

def decidir_numero(apos: Optional[int]):
    """
    Procura o melhor (padrão -> X) com:
      - suporte >= MIN_SUP
      - wilson_lower >= CONF_MIN
    Prioriza N maior; em empate, maior Wilson; depois maior suporte; depois menor X.
    Retorna: (numero_escolhido, padrao, N, wilson_low, p_hat, suporte, distribuicao_dict) ou None
    """
    cands = contexto_candidato(apos)
    for pad in cands:  # já vem N grande -> pequeno
        total = pattern_totals.get(pad, 0)
        if total < MIN_SUP:
            continue
        cnts = pattern_counts.get(pad, [0,0,0,0,0])
        # Distribuição e melhores
        best = None
        for x in (1,2,3,4):
            s = cnts[x]
            wl = wilson_lower(s, total)
            if wl >= CONF_MIN:
                p_hat = s / total
                item = (wl, p_hat, total, x)
                if (best is None) or (item > best):
                    best = item
        if best:
            wl, p_hat, total, x = best
            dist = {i: (cnts[i]/total) for i in (1,2,3,4)}
            return x, pad, len(pad), wl, p_hat, total, dist
    return None

# =========================
# Mensagens
# =========================
def fmt_dist(dist: Dict[int,float]) -> str:
    return " | ".join(f"{i}:{dist[i]*100:.1f}%" for i in (1,2,3,4))

async def enviar_sinal(x: int, pad: Tuple[int,...], n: int, wl: float, p_hat: float, total: int, dist: Dict[int,float], apos: Optional[int]):
    msg = (
        "🟢 <b>SINAL (NÚMERO SECO)</b>\n"
        f"🎯 Número: <b>{x}</b>\n"
        f"📍 Padrão: <b>{pad}</b> | N={n}"
        + (f" | Após: <b>{apos}</b>" if apos else "") + "\n"
        f"🧠 Conf. (Wilson 95%): <b>{wl*100:.1f}%</b>  (p̂={p_hat*100:.1f}% | amostra={total})\n"
        f"📈 Distribuição após {pad}: {fmt_dist(dist)}"
    )
    await bot.send_message(CHANNEL_ID, msg, parse_mode="HTML")

async def enviar_neutro(apos: Optional[int]):
    base_ctx = list(recent_nums)
    if apos is not None:
        base_ctx = (base_ctx + [apos]) if (not base_ctx or base_ctx[-1] != apos) else base_ctx
    ctx = tuple(base_ctx[-min(len(base_ctx), N_MAX):]) if base_ctx else ()
    msg = (
        "😐 <b>Neutro — avaliando combinações</b>\n"
        f"📍 Contexto atual: <b>{ctx if ctx else '—'}</b>\n"
        f"🔎 Nenhum padrão com Wilson ≥ {CONF_MIN*100:.0f}% e suporte ≥ {MIN_SUP} (N=1..{N_MAX})."
    )
    await bot.send_message(CHANNEL_ID, msg, parse_mode="HTML")

# =========================
# Relatórios
# =========================
def taxa(d): 
    d = list(d); 
    return (sum(d)/len(d)) if d else 0.0

def taxa_ultimos(d, n):
    d = list(d)[-n:]
    return (sum(d)/len(d)) if d else 0.0

async def envia_resumo():
    short_wr = taxa(hist_short)
    long_wr  = taxa(hist_long)
    lastN_wr = taxa_ultimos(hist_long, RESULTS_WIN)
    total    = state.get("greens_total",0)+state.get("reds_total",0)
    overall  = (state.get("greens_total",0)/total) if total else 0.0
    msg = (
        "📊 <b>Resumo</b>\n"
        f"🧪 Últimos {RESULTS_WIN}: <b>{lastN_wr*100:.1f}%</b>\n"
        f"⏱️ Curto (≈{len(hist_short)}): <b>{short_wr*100:.1f}%</b>\n"
        f"📚 Longo (≈{len(hist_long)}): <b>{long_wr*100:.1f}%</b>\n"
        f"🌐 Geral: <b>{overall*100:.1f}%</b> "
        f"({state.get('greens_total',0)}✅ / {state.get('reds_total',0)}❌)"
    )
    await bot.send_message(CHANNEL_ID, msg, parse_mode="HTML")

# =========================
# Handlers Telegram
# =========================
@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    info = (
        "🤖 Fantan — Modo <b>NÚMERO SECO</b> com padrões (n-grama)\n"
        f"• N_MAX={N_MAX} | MIN_SUP={MIN_SUP} | CONF_MIN={CONF_MIN:.2f} (Wilson 95%)\n"
        f"• SEND_SIGNALS={SEND_SIGNALS} (1 envia | 0 só prévia/neutro)\n"
        f"• REPORT_EVERY={REPORT_EVERY} | RESULTS_WINDOW={RESULTS_WIN}\n"
        "Comandos: /status"
    )
    await msg.answer(info, parse_mode="HTML")

@dp.message_handler(commands=["status"])
async def cmd_status(msg: types.Message):
    await envia_resumo()

@dp.channel_post_handler(content_types=["text"])
async def on_channel_post(message: types.Message):
    if message.chat.id != CHANNEL_ID:
        return
    txt = (message.text or "").strip()
    if not txt:
        return

    # 1) Alimenta modelo com SEQUÊNCIA (histórico completo)
    seq = extrai_sequencia(txt)
    if seq:
        alimentar_sequencia(seq)

    # 2) Resultado GREEN/RED (para resumo; não altera decisão do padrão)
    r = eh_resultado(txt)
    if r is not None:
        hist_long.append(r)
        hist_short.append(r)
        if r == 1: state["greens_total"] += 1
        else: state["reds_total"] += 1
        save_state()
        return

    # 3) SINAL → decidir número seco com base nas combinações
    if eh_sinal(txt):
        now = time.time()
        if now < state.get("cooldown_until", 0):
            return

        apos = extrai_apos(txt)  # pode ser None
        decisao = decidir_numero(apos)

        if decisao is None:
            # Sem padrão válido ≥90% → neutro
            await enviar_neutro(apos)
            state["cooldown_until"] = now + COOLDOWN_S
            save_state()
            return

        x, pad, n, wl, p_hat, total, dist = decisao

        if SEND_SIGNALS == 1:
            await enviar_sinal(x, pad, n, wl, p_hat, total, dist, apos)
            state["sinais_enviados"] += 1
            state["cooldown_until"] = now + COOLDOWN_S
            save_state()
            # resumo periódico
            if REPORT_EVERY > 0 and state["sinais_enviados"] % REPORT_EVERY == 0:
                await envia_resumo()
        else:
            # Modo teste: só imprime uma prévia discreta
            prev = (
                f"👀 Prévia (não enviado) — SECO {x} | pad={pad} N={n} | "
                f"wilson={wl*100:.1f}% | p̂={p_hat*100:.1f}% | N={total} | dist: {fmt_dist(dist)}"
            )
            await bot.send_message(CHANNEL_ID, prev, parse_mode="HTML")
            state["cooldown_until"] = now + COOLDOWN_S
            save_state()

# =========================
# FastAPI (webhook)
# =========================
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    if not PUBLIC_URL:
        log.warning("PUBLIC_URL não definido — webhook não será configurado.")
        return
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(f"{PUBLIC_URL}/webhook/{BOT_TOKEN}")

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.body()
    update = types.Update(**json.loads(data.decode("utf-8")))
    await dp.process_update(update)
    return {"ok": True}