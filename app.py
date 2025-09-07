import os
import re
import json
import time
import logging
from collections import deque
from aiogram import Bot, Dispatcher, executor, types

# =========================
# Configuração & Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("guardiao-risco-bot")

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHANNEL_ID = -1002810508717   # fixo no seu canal
CONF_LIMIAR = 0.99            # fixo

if not BOT_TOKEN:
    raise RuntimeError("Faltando variável TG_BOT_TOKEN.")

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
    except Exception:
        pass

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

load_state()

# =========================
# Buffers de aprendizado
# =========================
hist_long  = deque(maxlen=300)
hist_short = deque(maxlen=30)

ultimos_numeros = deque(maxlen=120)
contagem_num = [0, 0, 0, 0, 0]
transicoes   = [[0]*5 for _ in range(5)]

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
    if not (isinstance(depois_de, int) and 1 <= depois_de <= 4):
        tot_all = sum(contagem_num[1:5]) or 1
        return [0] + [contagem_num[i] / tot_all for i in range(1, 5)]
    total = sum(transicoes[depois_de][1:5])
    if total == 0:
        tot_all = sum(contagem_num[1:5]) or 1
        return [0] + [contagem_num[i] / tot_all for i in range(1, 5)]
    return [0] + [transicoes[depois_de][i] / total for i in range(1, 5)]

def risco_por_numeros(apos_num, alvos):
    if not alvos:
        return 0.5
    ultimo_referencia = apos_num if apos_num else (ultimos_numeros[-1] if ultimos_numeros else None)
    probs = probs_depois(ultimo_referencia)
    p_hit = sum(probs[a] for a in alvos if 1 <= a <= 4)
    return max(0.0, min(1.0, 1.0 - p_hit))

def conf_final(short_wr, long_wr, vol, max_reds, risco_num):
    base = 0.55*short_wr + 0.30*long_wr + 0.10*(1.0 - vol) + 0.05*(1.0 - risco_num)
    pena = 0.0
    if max_reds >= 3: pena += 0.05 * (max_reds - 2)
    if vol > 0.6:     pena += 0.05
    return max(0.0, min(1.0, base - pena))

# =========================
# Parsers
# =========================
re_sinal   = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
re_seq     = re.compile(r"Sequ[eê]ncia[:\s]*([^\n]+)", re.I)
re_apos    = re.compile(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", re.I)
re_apostar = re.compile(r"apostar\s+em\s+([A-Za-z]*\s*)?([1-4](?:[\s\-\|]*[1-4])*)", re.I)
re_red     = re.compile(r"\bRED\b", re.I)
re_close   = re.compile(r"APOSTA\s+ENCERRADA", re.I)

def eh_sinal(txt): return bool(re_sinal.search(txt or ""))
def extrai_sequencia(txt):
    m = re_seq.search(txt or ""); 
    return [int(x) for x in re.findall(r"[1-4]", m.group(1))] if m else []
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

# =========================
# Handler do Canal
# =========================
@dp.channel_post_handler(content_types=["text"])
async def on_channel_post(message: types.Message):
    if message.chat.id != CHANNEL_ID:
        return

    txt = (message.text or "").strip()
    if not txt:
        return

    # Sequência
    seq = extrai_sequencia(txt)
    if seq:
        atualiza_estat_num(seq)
        logger.info("Sequência aprendida: %s", seq)

    # Resultado
    r = eh_resultado(txt)
    if r is not None:
        hist_long.append(r); hist_short.append(r)
        logger.info("Resultado %s | WR30=%.1f%% WR300=%.1f%%",
                    "WIN" if r==1 else "RED",
                    winrate(hist_short)*100, winrate(hist_long)*100)
        return

    # Novo sinal
    if eh_sinal(txt):
        now = time.time()
        if now < state.get("cooldown_until", 0):
            await bot.send_message(CHANNEL_ID, "neutro")
            return

        short_wr = winrate(hist_short)
        long_wr  = winrate(hist_long)
        vol      = volatilidade(hist_short)
        mx_reds  = streak_loss(hist_short)

        apos_num, alvos = extrai_regra_sinal(txt)
        risco_num = risco_por_numeros(apos_num, alvos)
        conf = conf_final(short_wr, long_wr, vol, mx_reds, risco_num)

        logger.info("SINAL conf=%.3f | apos=%s alvos=%s risco_num=%.2f",
                    conf, apos_num, alvos, risco_num)

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
# Main
# =========================
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)