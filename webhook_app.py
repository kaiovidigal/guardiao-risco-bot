import os, json, re, time, threading
from typing import Dict, Any, Optional, List
from collections import defaultdict, deque
from datetime import datetime, timedelta

import nest_asyncio
nest_asyncio.apply()

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx

# ========================
# Config (via variáveis)
# ========================
TG_BOT_TOKEN      = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL    = os.getenv("PUBLIC_CHANNEL", "").strip()     # ex: @fantanvidigal
BOT_ENABLED       = os.getenv("BOT_ENABLED", "1").strip()       # "1" = manda msg pelo bot
Z_WILSON          = float(os.getenv("Z_WILSON", "1.96"))        # 95% conf
HIST_MAX          = int(os.getenv("HIST_MAX", "5000"))          # quantos eventos guardar
DECAY_DAYS        = float(os.getenv("DECAY_DAYS", "3"))         # meia-vida aproximada
MIN_OBS_FOR_CONF  = int(os.getenv("MIN_OBS_FOR_CONF", "30"))    # min. observações p/ conf estimada

BASE_URL = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI()

# ========================
# Estado em memória
# (Render tem FS efêmero; isto se perde em restart — simples e suficiente)
# ========================

# Estatística por número (1..4)
# stats[n] = deque([(timestamp, 1 ou 0), ...])  -> 1=acerto, 0=erro daquele número seco
stats: Dict[int, deque] = {i: deque(maxlen=HIST_MAX) for i in range(1, 5)}

# Guardar previsão por message_id do canal para casarmos com o fechamento
# predicted[message_id] = {"num": 2, "at": ts}
predicted: Dict[int, Dict[str, Any]] = {}

# Regex p/ extrair lista de números de 1..4 (2 | 3) / (2 3) / (2,3) etc
RE_NUMS = re.compile(r"\b([1-4])\b")

# Booleans para classificar fechamento
RE_GREEN = re.compile(r"\b(GREEN|VERDE)\b", re.I)
RE_RED   = re.compile(r"\b(RED|LOSS)\b", re.I)

# ========================
# Helpers
# ========================

def _now() -> float:
    return time.time()

def _decay_weight(ts: float, now: float) -> float:
    """peso exponencial ~ meia-vida DECAY_DAYS"""
    half_life = DECAY_DAYS * 24 * 3600
    return 0.5 ** ((now - ts) / max(1.0, half_life))

def score_number(n: int) -> Dict[str, float]:
    """Retorna métricas do número (p, n_eff, wilson_low) com decaimento."""
    now = _now()
    s = stats[n]
    if not s:
        return {"p": 0.5, "n_eff": 0.0, "wilson": 0.0}

    # soma ponderada
    w_sum = 0.0
    w_pos = 0.0
    for ts, ok in s:
        w = _decay_weight(ts, now)
        w_sum += w
        if ok:
            w_pos += w

    p = w_pos / max(1e-9, w_sum)

    # Wilson lower bound (aprox) com "n efetivo" = soma dos pesos
    n_eff = w_sum
    if n_eff <= 0:
        wilson = 0.0
    else:
        z = Z_WILSON
        # fórmula com n efetivo
        num = p + z*z/(2*n_eff) - z * ((p*(1-p)/n_eff + z*z/(4*n_eff*n_eff)) ** 0.5)
        den = 1 + z*z/n_eff
        wilson = num / den

    return {"p": p, "n_eff": n_eff, "wilson": max(0.0, min(1.0, wilson))}

def choose_single(numbers: List[int]) -> Dict[str, Any]:
    """
    Escolhe o “número seco” dentre numbers com base em:
    1) maior wilson
    2) desempate por maior p (média)
    3) desempate por maior n_eff
    """
    cand = []
    for n in numbers:
        m = score_number(n)
        cand.append((n, m["wilson"], m["p"], m["n_eff"]))
    cand.sort(key=lambda x: (x[1], x[2], x[3]), reverse=True)
    if not cand:
        # fallback
        return {"num": numbers[0], "chance": 0.5, "obs": 0}

    n, w, p, n_eff = cand[0]
    # chance estimada = wilson (mais conservadora)
    return {"num": n, "chance": w, "obs": int(round(n_eff))}

async def send_message(chat_id: int, text: str, parse_mode: Optional[str] = "HTML"):
    if BOT_ENABLED != "1":
        return
    async with httpx.AsyncClient(timeout=15) as cli:
        await cli.post(f"{BASE_URL}/sendMessage", data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode or ""
        })

def extract_numbers(text: str) -> List[int]:
    nums = [int(m.group(1)) for m in RE_NUMS.finditer(text)]
    # mantemos apenas 1..4 únicos, na ordem de aparição
    seen, out = set(), []
    for x in nums:
        if x not in seen and 1 <= x <= 4:
            seen.add(x)
            out.append(x)
    return out

def looks_like_entry(text: str) -> bool:
    return "ENTRADA" in text.upper()

def looks_like_close(text: str) -> bool:
    return ("APOSTA ENCERRADA" in text.upper()) or RE_GREEN.search(text) or RE_RED.search(text)

# ========================
# Webhook Telegram Bot
# ========================

def _validate_token(token_in_path: str):
    if TG_BOT_TOKEN == "" or token_in_path != TG_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")

@app.post("/webhook/{token}")
async def webhook_dyn(token: str, request: Request):
    _validate_token(token)
    data = await request.json()
    # Telegram envia updates de vários tipos; vamos focar em channel_post e message
    update = data
    msg = update.get("channel_post") or update.get("message")
    if not msg:
        return JSONResponse({"ok": True, "skip": "sem message/channel_post"})

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or ""

    # Filtra somente o canal público configurado (se informado)
    if PUBLIC_CHANNEL and (chat.get("username") or "") != PUBLIC_CHANNEL.lstrip("@"):
        # passa batido, mas seguimos logando
        return JSONResponse({"ok": True, "skip": "outro chat"})

    # 1) Sinal de ENTRADA -> escolher “número seco” e avisar
    if looks_like_entry(text):
        nums = extract_numbers(text)
        # Queremos entradas como “2 | 3” (2 números) ou “2 | 4 | 1” (3 números)
        nums = [n for n in nums if 1 <= n <= 4]
        # Evitar pegar números do “Placar do dia 26 1” etc:
        # regra simples: se houver 2 ou 3 números, tratamos como candidatos
        if 2 <= len(nums) <= 3:
            choice = choose_single(nums)
            seco = choice["num"]
            obs  = max(choice["obs"], sum(len(stats[i]) for i in range(1,5)))  # aproximação
            chance = choice["chance"]
            pct = f"{chance*100:.2f}%"

            # Guarda previsão por message_id (para casar com fechamento)
            message_id = msg.get("message_id")
            if message_id:
                predicted[message_id] = {"num": seco, "at": _now()}

            # Envia o alerta “número seco”
            out = (
                f"🎯 <b>Número seco sugerido:</b> <code>{seco}</code>\n"
                f"📊 <i>Baseado em ~{obs} sinais retroativos</i>\n"
                f"✅ Chance estimada: <b>{pct}</b>"
            )
            await send_message(chat_id, out)

            return JSONResponse({"ok": True, "sent": out, "candidates": nums})

    # 2) Mensagem de FECHAMENTO -> atualizar acerto/erro da última previsão
    if looks_like_close(text):
        # Busca “GREEN/VERDE” como acerto, “RED/LOSS” como erro
        ok = 1 if RE_GREEN.search(text) else (0 if RE_RED.search(text) else None)
        # Se não conseguimos identificar GREEN/RED, tentamos heurística:
        if ok is None:
            if "✅" in text: ok = 1
            elif "❌" in text: ok = 0

        # Tenta relacionar com a última previsão do mesmo chat.
        # Estratégia simples: pega o maior message_id anterior a este que tenha previsão
        msg_id = msg.get("message_id")
        guess_id = None
        if msg_id is not None:
            # mensagem mais recente anterior:
            keys = [k for k in predicted.keys() if k <= msg_id]
            if keys:
                guess_id = max(keys)

        if guess_id and guess_id in predicted and ok is not None:
            seco = predicted[guess_id]["num"]
            stats[seco].append((_now(), int(ok)))

        return JSONResponse({"ok": True, "close_recorded_for": predicted.get(guess_id, {})})

    return JSONResponse({"ok": True, "noop": True})

# ========================
# Status & saúde
# ========================

@app.get("/")
async def root():
    return {
        "ok": True,
        "conf": {
            "Z_WILSON": Z_WILSON,
            "HIST_MAX": HIST_MAX,
            "DECAY_DAYS": DECAY_DAYS,
            "MIN_OBS_FOR_CONF": MIN_OBS_FOR_CONF
        },
        "bot": {"enabled": BOT_ENABLED == "1"},
        "webhook": "/webhook"
    }

@app.get("/status")
async def status():
    now = _now()
    table = {}
    for n in range(1, 5):
        m = score_number(n)
        table[n] = {
            "p_media": round(m["p"], 4),
            "n_eff": round(m["n_eff"], 1),
            "wilson_low": round(m["wilson"], 4),
            "last_events": len(stats[n]),
        }
    return {"ok": True, "stats": table, "now": now}

# ========================
# Inicialização (somente API do bot — o scraper já está no worker atual)
# ========================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("webhook_app:app", host="0.0.0.0", port=port)