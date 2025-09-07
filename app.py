import os
import re
import json
import time
import logging
from collections import deque
from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.exceptions import ChatNotFound, Unauthorized

# =========================
# Configuração & Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("guardiao-risco-bot")

BOT_TOKEN = os.getenv("TG_BOT_TOKEN") or os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("TG_CHAT_ID")  # ex.: -1002810508717
CONF_LIMIAR = float(os.getenv("CONF_LIMIAR", "0.99"))  # confiança mínima para ENTRAR
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")  # opcional

if not BOT_TOKEN:
    raise RuntimeError("Faltando variável de ambiente TG_BOT_TOKEN (ou BOT_TOKEN).")
if not CHANNEL_ID:
    raise RuntimeError("Faltando variável de ambiente TG_CHAT_ID (id do canal, ex.: -100...).")

try:
    CHANNEL_ID = int(CHANNEL_ID)
except ValueError:
    raise RuntimeError("TG_CHAT_ID deve ser um número inteiro (ex.: -1001234567890).")

if TARGET_CHAT_ID:
    try:
        TARGET_CHAT_ID = int(TARGET_CHAT_ID)
    except ValueError:
        logger.warning("TARGET_CHAT_ID inválido; ignorando.")
        TARGET_CHAT_ID = None

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

# =========================
# Persistência simples
# =========================
STATE_FILE = os.getenv("STATE_FILE", "data/state.json")
os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)

state = {
    "cooldown_until": 0.0,
    "limiar": CONF_LIMIAR,
}

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                state.update(data)
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
# Histórico de resultados (1=WIN, 0=RED)
hist_long  = deque(maxlen=300)
hist_short = deque(maxlen=30)

# Fantan: números 1..4, sequência e transições
ultimos_numeros = deque(maxlen=120)   # sequência bruta 1..4
contagem_num = [0, 0, 0, 0, 0]        # índice 1..4
transicoes   = [[0]*5 for _ in range(5)]  # 5x5, usa 1..4

def atualiza_estat_num(seq_nums):
    global ultimos_numeros, contagem_num, transicoes
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
    """Probabilidade empírica do próximo número 1..4 dado o último 'depois_de'."""
    if not (isinstance(depois_de, int) and 1 <= depois_de <= 4):
        # fallback: frequência global
        tot_all = sum(contagem_num[1:5]) or 1
        return [0] + [contagem_num[i] / tot_all for i in range(1, 5)]
    total = sum(transicoes[depois_de][1:5])
    if total == 0:
        # sem transições registradas, volta à frequência global
        tot_all = sum(contagem_num[1:5]) or 1
        return [0] + [contagem_num[i] / tot_all for i in range(1, 5)]
    return [0] + [transicoes[depois_de][i] / total for i in range(1, 5)]

def risco_por_numeros(apos_num, alvos):
    """Risco numérico = 1 - probabilidade de acerto dos alvos, dado o último número."""
    if not alvos:
        return 0.5  # neutro na falta de informação
    ultimo_referencia = apos_num if apos_num else (ultimos_numeros[-1] if ultimos_numeros else None)
    probs = probs_depois(ultimo_referencia)
    p_hit = sum(probs[a] for a in alvos if 1 <= a <= 4)
    return max(0.0, min(1.0, 1.0 - p_hit))

def conf_final(short_wr, long_wr, vol, max_reds, risco_num):
    """
    Combina desempenho recente (short/long), estabilidade (1-vol),
    penaliza sequência de RED e volatilidade alta, e pondera risco numérico.
    """
    base = 0.55*short_wr + 0.30*long_wr + 0.10*(1.0 - vol) + 0.05*(1.0 - risco_num)
    pena = 0.0
    if max_reds >= 3: pena += 0.05 * (max_reds - 2)
    if vol > 0.6:     pena += 0.05
    conf = max(0.0, min(1.0, base - pena))
    return conf

# =========================
# Detectores / Parsers
# =========================
# Sinal novo
re_sinal = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)

# Sequência: "Sequência: 2 | 1" (ou "2-1", etc)
re_seq = re.compile(r"Sequ[eê]ncia[:\s]*([^\n]+)", re.I)

# "Entrar após o 2"
re_apos = re.compile(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", re.I)

# "apostar em Ssh 4-3-2" → ignora palavras, pega só dígitos 1..4
re_apostar = re.compile(r"apostar\s+em\s+([A-Za-z]*\s*)?([1-4](?:[\s\-\|]*[1-4])*)", re.I)

# Resultado RED (com "APOSTA ENCERRADA" e/ou RED ❌)
re_red = re.compile(r"\bRED\b", re.I)
re_close = re.compile(r"APOSTA\s+ENCERRADA", re.I)

def eh_sinal(texto: str) -> bool:
    return bool(re_sinal.search(texto or ""))

def extrai_sequencia(texto: str):
    m = re_seq.search(texto or "")
    if not m:
        return []
    nums = re.findall(r"[1-4]", m.group(1))
    return [int(x) for x in nums]

def extrai_regra_sinal(texto: str):
    """Retorna (apos_num, alvos:list[int]) de algo como:
       'Entrar após o 2 apostar em Ssh 4-3-2' """
    up = texto or ""
    m1 = re_apos.search(up)
    m2 = re_apostar.search(up)
    apos = int(m1.group(1)) if m1 else None
    alvos = []
    if m2:
        alvos = [int(x) for x in re.findall(r"[1-4]", m2.group(2))]
    return (apos, alvos)

def eh_resultado(texto: str):
    """1 = WIN/GREEN, 0 = RED, None = não sei."""
    up = (texto or "").upper()
    if re_red.search(up) or re_close.search(up):
        return 0
    if "GREEN" in up or "WIN" in up or "✅" in up:
        return 1
    return None

# =========================
# Utilitário de publicação
# =========================
async def publicar(texto, origem_id):
    destino = TARGET_CHAT_ID if TARGET_CHAT_ID else origem_id
    try:
        await bot.send_message(destino, texto)
    except (ChatNotFound, Unauthorized) as e:
        logger.error("Destino inválido (%s). Texto='%s'", e, texto)
    except Exception as e:
        logger.error("Falha ao publicar decisão: %s", e)

# =========================
# Handlers
# =========================
@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    await msg.answer(
        "🤖 Guardião de Risco ativo!\n"
        "• Ouvindo o canal configurado.\n"
        f"• Limiar atual: {state.get('limiar', CONF_LIMIAR):.2f}\n"
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
        f"Limiar: {state.get('limiar', CONF_LIMIAR):.2f}"
    )

@dp.channel_post_handler(content_types=["text"])
async def on_channel_post(message: types.Message):
    if message.chat.id != CHANNEL_ID:
        return

    txt = (message.text or "").strip()
    if not txt:
        return

    # 0) Aprender sequência Fantan
    seq = extrai_sequencia(txt)
    if seq:
        atualiza_estat_num(seq)
        logger.info("Sequência aprendida: %s (últ=%s)", seq, (ultimos_numeros[-1] if ultimos_numeros else None))

    # 1) Aprender resultado (WIN/RED)
    r = eh_resultado(txt)
    if r is not None:
        hist_long.append(r)
        hist_short.append(r)
        logger.info("Resultado %s | WR30=%.1f%% WR300=%.1f%%",
                    "WIN" if r==1 else "RED",
                    winrate(hist_short)*100, winrate(hist_long)*100)
        return

    # 2) Sinal novo
    if eh_sinal(txt):
        now = time.time()
        if now < state.get("cooldown_until", 0):
            await publicar("neutro", message.chat.id)
            return

        short_wr = winrate(hist_short)
        long_wr  = winrate(hist_long)
        vol      = volatilidade(hist_short)
        mx_reds  = streak_loss(hist_short)

        apos_num, alvos = extrai_regra_sinal(txt)
        risco_num = risco_por_numeros(apos_num, alvos)
        conf = conf_final(short_wr, long_wr, vol, mx_reds, risco_num)

        logger.info(
            "SINAL conf=%.3f | WR30=%.2f WR300=%.2f vol=%.2f reds=%d | apos=%s alvos=%s risco_num=%.2f",
            conf, short_wr, long_wr, vol, mx_reds, apos_num, alvos, risco_num
        )

        if conf >= state.get("limiar", CONF_LIMIAR):
            msg = (
                f"🎯 Chance: {conf*100:.1f}%\n"
                f"🛡️ Risco: BAIXO\n"
                f"🎯 Alvos: {('-'.join(map(str, alvos)) if alvos else '—')}\n"
                f"📍 Plano: ENTRAR (até G1)"
            )
        else:
            msg = "neutro"

        await publicar(msg, message.chat.id)
        state["cooldown_until"] = now + 20  # 20s anti-flood
        save_state()

# =========================
# Main
# =========================
if __name__ == "__main__":
    # skip_updates=True ajuda a evitar conflitos com sessões antigas
    executor.start_polling(dp, skip_updates=True)
