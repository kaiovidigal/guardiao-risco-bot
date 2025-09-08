# webhook_app.py — Guardião Fantan com “número seco” (1–4) ou multi-alvos

import os, re, json, time, logging
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
logger = logging.getLogger("guardiao-fantan")

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Faltando TG_BOT_TOKEN")

CHANNEL_ID      = int(os.getenv("CHANNEL_ID", "-1002810508717"))  # canal de sinais
COOLDOWN_S      = int(os.getenv("COOLDOWN_S", "20"))              # anti-flood
MODO_RECOM      = (os.getenv("MODO_RECOMENDACAO", "AUTO") or "AUTO").upper()  # AUTO|SECO|MULTI
MIN_PROB_SECO   = float(os.getenv("MIN_PROB_SECO", "0.33"))       # prob mínima p/ enviar nº seco
TOPK_MULTI      = int(os.getenv("TOPK_MULTI", "3"))               # qtos números no multi (quando necessário)
ALPHA_SUAV      = float(os.getenv("ALPHA_SUAV", "1.0"))           # suavização Laplace
MIN_TRANSICOES  = int(os.getenv("MIN_TRANSICOES", "8"))           # transições mínimas p/ confiar no "depois de X"
CONF_MIN_ENVIO  = float(os.getenv("CONF_MIN_ENVIO", "0.0"))       # confiança mínima (0..1) para enviar

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp  = Dispatcher(bot)

# =========================
# Persistência simples
# =========================
STATE_FILE = "data/state.json"
os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
state = {"cooldown_until": 0.0}

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            state.update(json.load(open(STATE_FILE, "r", encoding="utf-8")))
    except Exception as e:
        logger.warning("Falha ao carregar state: %s", e)

def save_state():
    try:
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Falha ao salvar state: %s", e)

load_state()

# =========================
# Buffers de aprendizado
# =========================
hist_long  = deque(maxlen=300)   # histórico de greens/reds
hist_short = deque(maxlen=30)

ultimos_numeros = deque(maxlen=120)         # sequência bruta 1..4
contagem_num    = [0, 0, 0, 0, 0]           # frequência global por nº
transicoes      = [[0]*5 for _ in range(5)] # transicoes[a][b] = qtde de b após a

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
# Métricas auxiliares
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
    s = 0; mx = 0
    for x in d:
        if x == 0:
            s += 1; mx = max(mx, s)
        else:
            s = 0
    return mx

# =========================
# Distribuições condicionais
# =========================
def _dist_global():
    tot = sum(contagem_num[1:5]) + 4*ALPHA_SUAV
    return [0] + [(contagem_num[i] + ALPHA_SUAV) / tot for i in range(1, 5)]

def _dist_condicional(depois_de):
    if not (isinstance(depois_de, int) and 1 <= depois_de <= 4):
        return _dist_global()
    total = sum(transicoes[depois_de][1:5])
    if total < MIN_TRANSICOES:
        return _dist_global()
    tot = total + 4*ALPHA_SUAV
    return [0] + [(transicoes[depois_de][i] + ALPHA_SUAV) / tot for i in range(1, 5)]

def probs_depois(depois_de):
    """Distribuição P(número | depois_de). Cai em global se dados insuficientes."""
    return _dist_condicional(depois_de)

# =========================
# Score de risco/confiança
# =========================
def risco_por_alvos(apos_num, alvos):
    if not alvos:
        return 0.5
    ultimo_ref = apos_num if apos_num else (ultimos_numeros[-1] if ultimos_numeros else None)
    probs = probs_depois(ultimo_ref)
    p_hit = sum(probs[a] for a in alvos if 1 <= a <= 4)
    return 1.0 - p_hit

def conf_final(short_wr, long_wr, vol, max_reds, risco_alvos):
    base = 0.55*short_wr + 0.30*long_wr + 0.10*(1.0 - vol) + 0.05*(1.0 - risco_alvos)
    pena = 0.0
    if max_reds >= 3: pena += 0.05 * (max_reds - 2)
    if vol > 0.6:     pena += 0.05
    return max(0.0, min(1.0, base - pena))

# =========================
# Parsers
# =========================
re_sinal     = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
re_seq       = re.compile(r"Sequ[eê]ncia[:\s]*([^\n]+)", re.I)
re_apos      = re.compile(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", re.I)
re_apostar   = re.compile(r"apostar\s+em\s+([A-Za-z]*\s*)?([1-4](?:[\s\-\|]*[1-4])*)", re.I)
re_close     = re.compile(r"APOSTA\s+ENCERRADA", re.I)
re_green     = re.compile(r"\bGREEN\b|✅", re.I)
re_red       = re.compile(r"\bRED\b|❌", re.I)
re_modo_seco = re.compile(r"\b(seco|1\s*n[uú]mero)\b", re.I)

def eh_sinal(txt): 
    return bool(re_sinal.search(txt or ""))

def extrai_sequencia(txt):
    m = re_seq.search(txt or "")
    return [int(x) for x in re.findall(r"[1-4]", m.group(1))] if m else []

def extrai_regra_sinal(txt):
    m1 = re_apos.search(txt or "")
    m2 = re_apostar.search(txt or "")
    apos = int(m1.group(1)) if m1 else None
    alvos = [int(x) for x in re.findall(r"[1-4]", (m2.group(2) if m2 else ""))]
    return (apos, alvos)

def eh_resultado(txt):
    up = (txt or "").upper()
    if not re_close.search(up): return None
    if re_green.search(up): return 1
    if re_red.search(up):   return 0
    return None

def pedido_modo_seco(txt):
    return bool(re_modo_seco.search(txt or ""))

# =========================
# Regras de recomendação
# =========================
def escolhe_numero_seco(apos_num):
    """Retorna (numero, probabilidade) do melhor nº 1..4 após 'apos_num' (ou global)."""
    ref = apos_num if apos_num else (ultimos_numeros[-1] if ultimos_numeros else None)
    p = probs_depois(ref)
    # argmax entre 1..4
    melhor = max(range(1,5), key=lambda i: p[i])
    return melhor, p[melhor]

def escolhe_multi_alvos(apos_num, k=TOPK_MULTI):
    """Retorna top-k números por probabilidade após 'apos_num' (ou global)."""
    ref = apos_num if apos_num else (ultimos_numeros[-1] if ultimos_numeros else None)
    p = probs_depois(ref)
    ordem = sorted(range(1,5), key=lambda i: p[i], reverse=True)
    return ordem[:max(1, min(4, k))], [p[i] for i in ordem[:max(1, min(4, k))]]

def define_modo_recomendacao(txt):
    if MODO_RECOM in ("SECO", "MULTI"):
        return MODO_RECOM
    # AUTO: decide pelo conteúdo do texto
    return "SECO" if pedido_modo_seco(txt) else "MULTI"

# =========================
# Handlers
# =========================
@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    info = (
        "🤖 Guardião Fantan ativo!\n\n"
        "• Modos: <b>SECO</b> (1 número) ou <b>MULTI</b> (2–3 números).\n"
        "• Ambiente: MODO_RECOMENDACAO=AUTO|SECO|MULTI, "
        f"MIN_PROB_SECO={MIN_PROB_SECO}, TOPK_MULTI={TOPK_MULTI}.\n"
        "• Para forçar número seco no texto, inclua: “seco” ou “1 número”."
    )
    await msg.answer(info)

@dp.channel_post_handler(content_types=["text"])
async def on_channel_post(message: types.Message):
    if message.chat.id != CHANNEL_ID: 
        return
    txt = (message.text or "").strip()
    if not txt: 
        return

    # 1) Alimenta estatística com sequência bruta (se houver)
    seq = extrai_sequencia(txt)
    if seq: 
        atualiza_estat_num(seq)

    # 2) Resultado de aposta (GREEN/RED)
    r = eh_resultado(txt)
    if r is not None:
        hist_long.append(r)
        hist_short.append(r)
        return

    # 3) Novo SINAL
    if eh_sinal(txt):
        now = time.time()
        if now < state.get("cooldown_until", 0): 
            return

        apos_num, alvos_txt = extrai_regra_sinal(txt)
        modo = define_modo_recomendacao(txt)

        # Gera recomendação de acordo com o modo + o que veio no sinal
        if modo == "SECO":
            num, pnum = escolhe_numero_seco(apos_num)
            alvos = [num]
            tipo = "NÚMERO SECO"
            prob_info = f"{num}:{pnum*100:.1f}%"
            # opcional: exigir prob mínima
            if pnum < MIN_PROB_SECO:
                logger.info("Probabilidade (%.3f) abaixo do mínimo (%.3f). Sinal não enviado.", pnum, MIN_PROB_SECO)
                return
        else:  # MULTI
            if alvos_txt:  # se o sinal já veio com alvos, usa-os
                alvos = [a for a in alvos_txt if 1 <= a <= 4]
                # ainda mostramos probabilidades por nº
                p = probs_depois(apos_num if apos_num else (ultimos_numeros[-1] if ultimos_numeros else None))
                prob_info = " | ".join(f"{i}:{p[i]*100:.1f}%" for i in range(1,5))
            else:
                # gerar automaticamente top-k
                alvos, pks = escolhe_multi_alvos(apos_num, k=TOPK_MULTI)
                prob_info = " | ".join(f"{a}:{pk*100:.1f}%" for a, pk in zip(alvos, pks))
            tipo = "MULTI-ALVOS"

        # 4) Métricas e confiança
        short_wr = winrate(hist_short); long_wr = winrate(hist_long)
        vol      = volatilidade(hist_short); mx_reds = streak_loss(hist_short)
        risco    = risco_por_alvos(apos_num, alvos)
        conf     = conf_final(short_wr, long_wr, vol, mx_reds, risco)

        if conf < CONF_MIN_ENVIO:
            logger.info("Confiança (%.3f) abaixo de CONF_MIN_ENVIO (%.3f). Sinal não enviado.", conf, CONF_MIN_ENVIO)
            return

        # 5) Mensagem
        p_all = probs_depois(apos_num if apos_num else (ultimos_numeros[-1] if ultimos_numeros else None))
        probs_txt = " | ".join(f"{i}:{p_all[i]*100:.1f}%" for i in range(1,5))

        msg_out = (
            f"🟢 <b>SINAL ({tipo})</b>\n"
            f"🎯 Recomendação: <b>{'-'.join(map(str, alvos)) if alvos else '—'}</b>\n"
            f"📍 Após: <b>{apos_num if apos_num else '—'}</b>\n"
            f"📊 Confiança: <b>{conf*100:.1f}%</b>\n"
            f"🧠 Prob. alvo(s): {prob_info}\n"
            f"📈 Prob. por nº: {probs_txt}"
        )

        await bot.send_message(CHANNEL_ID, msg_out, parse_mode="HTML")
        state["cooldown_until"] = now + COOLDOWN_S
        save_state()

# =========================
# FastAPI
# =========================
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    base_url = (os.getenv("PUBLIC_URL") or "").rstrip("/")
    if not base_url: 
        return
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(f"{base_url}/webhook/{BOT_TOKEN}")

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.body()
    update = types.Update(**json.loads(data.decode("utf-8")))
    await dp.process_update(update)
    return {"ok": True}