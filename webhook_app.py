import os
import re
import json
import time
import logging
from collections import deque
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types

# =========================
# Config & Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("guardiao-risco-bot")

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Faltando TG_BOT_TOKEN")

CHANNEL_ID = -1002810508717   # seu canal fixo
CONF_LIMIAR = 0.92            # entra a partir de 92%

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

# =========================
# Persistência simples
# =========================
STATE_FILE = "data/state.json"
os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)

state = {"cooldown_until": 0.0, "limiar": CONF_LIMIAR}

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state.update(json.load(f))
    except Exception as e:
        logger.warning("Falha ao carregar state: %s", e)

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Falha ao salvar state: %s", e)

load_state()

# =========================
# Buffers de aprendizado
# =========================
hist_long  = deque(maxlen=300)   # últimos 300 resultados (0/1)
hist_short = deque(maxlen=30)    # últimos 30 resultados (0/1)

ultimos_numeros = deque(maxlen=120)   # números 1..4 na ordem observada
contagem_num = [0, 0, 0, 0, 0]        # frequência por número (índices 1..4)
transicoes   = [[0]*5 for _ in range(5)]  # contagem de transições prev->n (índices 1..4)

def atualiza_estat_num(seq_nums):
    for n in seq_nums:
        if 1 <= n <= 4:
            if ultimos_numeros:
                prev = ultimos_numeros[-1]
                if 1 <= prev <= 4:
                    transicoes[prev][n] += 1
            ultimos_numeros.append(n)
            contagem_num[n] += 1

# =========================
# Métricas de risco
# =========================
def winrate(d):
    d = list(d)
    return (sum(d) / len(d)) if d else 0.0

def volatilidade(d):
    d = list(d)
    if len(d) < 10:
        return 0.0
    trocas = sum(1 for i in range(1, len(d)) if d[i] != d[i-1])
    return trocas / (len(d) - 1)

def streak_loss(d):
    d = list(d)
    s = 0; mx = 0
    for x in d:
        if x == 0:
            s += 1
            mx = max(mx, s)
        else:
            s = 0
    return mx

def probs_depois(depois_de):
    """
    Retorna vetor [0, p(1), p(2), p(3), p(4)]
    com suavização de Laplace (alpha=1) para evitar extremos 0/1.
    Se não houver dados suficientes para 'depois_de', usa a distribuição global.
    """
    alpha = 1.0

    def dist_global():
        tot = sum(contagem_num[1:5]) + 4*alpha
        return [0] + [(contagem_num[i] + alpha) / tot for i in range(1, 5)]

    if not (isinstance(depois_de, int) and 1 <= depois_de <= 4):
        return dist_global()

    total = sum(transicoes[depois_de][1:5])
    if total < 8:  # exige no mínimo 8 observações da transição específica
        return dist_global()

    tot = total + 4*alpha
    return [0] + [(transicoes[depois_de][i] + alpha) / tot for i in range(1, 5)]

def risco_por_numeros(apos_num, alvos):
    """
    Retorna risco = 1 - p_hit para os alvos, dado o último número (apos_num)
    ou o último observado em ultimos_numeros se apos_num for None.
    """
    if not alvos:
        return 0.5
    ultimo_ref = apos_num if apos_num else (ultimos_numeros[-1] if ultimos_numeros else None)
    probs = probs_depois(ultimo_ref)
    p_hit = sum(probs[a] for a in alvos if 1 <= a <= 4)
    return max(0.0, min(1.0, 1.0 - p_hit))

def conf_final(short_wr, long_wr, vol, max_reds, risco_num):
    """
    Combina WR curto/longo, estabilidade e risco numérico em uma confiança [0..1].
    Penaliza streaks de RED >=3 e volatilidade muito alta (>0.6).
    """
    base = 0.55*short_wr + 0.30*long_wr + 0.10*(1.0 - vol) + 0.05*(1.0 - risco_num)
    pena = 0.0
    if max_reds >= 3:
        pena += 0.05 * (max_reds - 2)
    if vol > 0.6:
        pena += 0.05
    return max(0.0, min(1.0, base - pena))

# =========================
# Parsers / Detectores
# =========================
re_sinal   = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
re_seq     = re.compile(r"Sequ[eê]ncia[:\s]*([^\n]+)", re.I)
re_apos    = re.compile(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", re.I)
re_apostar = re.compile(r"apostar\s+em\s+([A-Za-z]*\s*)?([1-4](?:[\s\-\|]*[1-4])*)", re.I)
re_red     = re.compile(r"\bRED\b", re.I)
re_close   = re.compile(r"APOSTA\s+ENCERRADA", re.I)

def eh_sinal(txt): 
    return bool(re_sinal.search(txt or ""))

def extrai_sequencia(txt):
    m = re_seq.search(txt or "")
    if not m: return []
    return [int(x) for x in re.findall(r"[1-4]", m.group(1))]

def extrai_regra_sinal(txt):
    m1 = re_apos.search(txt or "")
    m2 = re_apostar.search(txt or "")
    apos = int(m1.group(1)) if m1 else None
    alvos = [int(x) for x in re.findall(r"[1-4]", m2.group(2))] if m2 else []
    return (apos, alvos)

def eh_resultado(txt):
    up = (txt or "").upper()
    if re_red.search(up) or re_close.search(up): return 0
    if "GREEN" in up or "WIN" in up or "✅" in up: return 1
    return None

# =====================
# Anti-loss por repetição
# =====================
def risco_repeticao(apos_num, alvos):
    """
    Bloqueia entradas arriscadas quando o número tende a se repetir
    e o sinal exclui justamente esse número. Ex.: após 1 → apostar em 2-3-4, mas p(1|1) alta.
    """
    if apos_num not in [1, 2, 3, 4]:
        return False
    probs = probs_depois(apos_num)   # [0, p1, p2, p3, p4]
    p_repeat = probs[apos_num]
    # Limiar de repetição (40%); se o próprio número não está nos alvos, evite
    if p_repeat >= 0.40 and apos_num not in alvos:
        return True
    return False

# =========================
# Handlers
# =========================
@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    await msg.answer(
        "🤖 Guardião de Risco (webhook) ativo!\n"
        f"• Canal monitorado: {CHANNEL_ID}\n"
        f"• Limiar: {CONF_LIMIAR:.2f}\n"
        "• Use /status para ver métricas."
    )

@dp.message_handler(commands=["status"])
async def cmd_status(msg: types.Message):
    short_wr = winrate(hist_short)
    long_wr  = winrate(hist_long)
    vol      = volatilidade(hist_short)
    reds     = streak_loss(hist_short)
    ultimo   = ultimos_numeros[-1] if ultimos_numeros else None
    await msg.answer(
        "📊 Status:\n"
        f"WR30: {short_wr*100:.1f}% | WR300: {long_wr*100:.1f}%\n"
        f"Volatilidade: {vol:.2f} | Max REDs: {reds}\n"
        f"Último número Fantan: {ultimo}\n"
        f"Limiar: {CONF_LIMIAR:.2f}"
    )

# ====== Handler do CANAL (principal) ======
@dp.channel_post_handler(content_types=["text"])
async def on_channel_post(message: types.Message):
    if message.chat.id != CHANNEL_ID:
        return

    txt = (message.text or "").strip()
    if not txt:
        return

    # 0) Aprender sequência Fantan (atualiza contagens e transições)
    seq = extrai_sequencia(txt)
    if seq:
        atualiza_estat_num(seq)
        logger.info("Sequência aprendida: %s (últ=%s)", seq, (ultimos_numeros[-1] if ultimos_numeros else None))

    # 1) Aprender resultado (RED/WIN)
    r = eh_resultado(txt)
    if r is not None:
        hist_long.append(r)
        hist_short.append(r)
        logger.info("Resultado %s | WR30=%.1f%% WR300=%.1f%%",
                    "WIN" if r==1 else "RED",
                    winrate(hist_short)*100, winrate(hist_long)*100)
        return

    # 2) Sinal novo (ENTRADA CONFIRMADA)
    if eh_sinal(txt):
        now = time.time()
        if now < state.get("cooldown_until", 0):
            await bot.send_message(CHANNEL_ID, "neutro")
            return

        apos_num, alvos = extrai_regra_sinal(txt)

        # 🔒 Regra anti-loss por repetição — se arriscado, indica qual número
        if risco_repeticao(apos_num, alvos):
            logger.info("Bloqueado padrão repetitivo: após %s ignorando ele mesmo", apos_num)
            await bot.send_message(CHANNEL_ID, f"neutro (número repetitivo: {apos_num})")
            return

        # Métricas
        short_wr = winrate(hist_short)
        long_wr  = winrate(hist_long)
        vol      = volatilidade(hist_short)
        mx_reds  = streak_loss(hist_short)

        # Probabilidade de acerto pelos alvos (p_hit) → risco = 1 - p_hit
        risco_num = risco_por_numeros(apos_num, alvos)
        conf = conf_final(short_wr, long_wr, vol, mx_reds, risco_num)

        logger.info(
            "SINAL conf=%.3f | WR30=%.2f WR300=%.2f vol=%.2f reds=%d | apos=%s alvos=%s risco_num=%.2f",
            conf, short_wr, long_wr, vol, mx_reds, apos_num, alvos, risco_num
        )

        if conf >= CONF_LIMIAR:
            msg = (
                f"🎯 Chance: {conf*100:.1f}%\n"
                f"🛡️ Risco: BAIXO\n"
                f"🎯 Alvos: {('-'.join(map(str, alvos)) if alvos else '—')}\n"
                f"📍 Plano: ENTRAR (até G1)"
            )
        else:
            msg = "neutro"

        await bot.send_message(CHANNEL_ID, msg)
        state["cooldown_until"] = now + 20
        save_state()

# =========================
# FastAPI (Webhook)
# =========================
app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    # Remove webhook antigo e configura o novo endpoint
    base_url = (os.getenv("PUBLIC_URL") or "").rstrip("/")  # evita //webhook
    if not base_url:
        logger.warning("PUBLIC_URL não definido; defina no Render (ex.: https://seuservico.onrender.com)")
        return
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(f"{base_url}/webhook/{BOT_TOKEN}")
    logger.info("Webhook configurado em %s/webhook/<token>", base_url)

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.body()
    update = types.Update(**json.loads(data.decode("utf-8")))
    await dp.process_update(update)
    return {"ok": True}