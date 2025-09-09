# webhook_app.py
import json, re, time, asyncio
from typing import Any, Dict, List, Optional, Tuple
from fastapi import FastAPI, Request
import httpx

# =======================
# CONFIG embutida
# =======================
CONFIG = {
    # Canal onde o BOT vai postar a sugestão (saída)
    "TARGET_CHANNEL": "@SEU_CANAL_SAIDA",      # ex.: "@fantanvidigal" ou id -1002810...
    # Canal de onde vêm os sinais (só para telemetria/checagem leve)
    "PUBLIC_CHANNEL": "@SEU_CANAL_SINAIS",

    # Token do BOT que posta
    "TG_BOT_TOKEN": "8217345207:COLOQUE_SEU_TOKEN_AQUI",

    # Segurança simples do webhook: o mesmo token no path
    "WEBHOOK_TOKEN": "8217345207:COLOQUE_SEU_TOKEN_AQUI",

    # Modo de sugestão (2 = número seco com aprendizado + fallback)
    "SUGGEST_MODE": 2,

    # Nunca pular sinais
    "CONF_MIN": 0.0,
    "COOLDOWN_S": 0,
    "DEDUP_MINUTES": 0,

    # Persistência
    "MEMORY_PATH": "memory.json",
}

# =======================
# Memória (persistente em arquivo)
# =======================
def load_memory(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "freq": {},            # chave -> {"total": int, "counts": {"1":..,"2":..,"3":..,"4":..}}
            "pending": {}          # strategy_id -> {"offered":[...], "ts": float}
        }

def save_memory(path: str, data: Dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[MEM][EXC] {e}")

MEM = load_memory(CONFIG["MEMORY_PATH"])

# =======================
# Util
# =======================
def norm_offered(nums: List[int]) -> List[int]:
    return sorted([n for n in nums if n in (1,2,3,4)])

def key_for(strategy_id: Optional[str], offered: List[int]) -> str:
    offered_key = ",".join(map(str, norm_offered(offered)))
    sid = strategy_id or "NA"
    return f"{sid}|{offered_key}"

def wilson_p(count: int, total: int, z: float = 1.96) -> float:
    if total <= 0: return 0.0
    p = count / total
    denom = 1 + (z**2)/total
    num   = p + (z**2)/(2*total)
    rad   = z * ((p*(1-p) + (z**2)/(4*total)) / total) ** 0.5
    lower = (num - rad) / denom
    upper = (num + rad) / denom
    return max(0.0, min(1.0, upper))

# =======================
# Parsers
# =======================
RE_TRIO = re.compile(r"Sequ[eê]ncia\s*:\s*(\d)\s*\|\s*(\d)\s*\|\s*(\d)", re.IGNORECASE)
RE_PAIR = re.compile(r"Sequ[eê]ncia\s*:\s*(\d)\s*\|\s*(\d)", re.IGNORECASE)
RE_STRAT= re.compile(r"Estrat[eé]gia\s*:\s*(\d+)", re.IGNORECASE)
RE_GREEN= re.compile(r"GREEN.+?\((\d)\)", re.IGNORECASE)   # ex: GREEN!!! (3)
RE_REDWN= re.compile(r"VENCEDOR\s*:\s*(\d)", re.IGNORECASE) # opcional se tiver "VENCEDOR: x"

def extract_offered(text: str) -> Optional[List[int]]:
    m = RE_TRIO.search(text)
    if m: return [int(m.group(1)), int(m.group(2)), int(m.group(3))]
    m = RE_PAIR.search(text)
    if m: return [int(m.group(1)), int(m.group(2))]
    return None

def extract_strategy(text: str) -> Optional[str]:
    m = RE_STRAT.search(text)
    return m.group(1) if m else None

def extract_winner(text: str) -> Optional[int]:
    m = RE_GREEN.search(text)
    if m: return int(m.group(1))
    m = RE_REDWN.search(text)
    if m: return int(m.group(1))
    return None

# =======================
# Aprendizado
# =======================
def remember_offer(strategy_id: Optional[str], offered: List[int]):
    try:
        if not offered: return
        if strategy_id:
            MEM["pending"][strategy_id] = {"offered": norm_offered(offered), "ts": time.time()}
            save_memory(CONFIG["MEMORY_PATH"], MEM)
    except Exception as e:
        print(f"[MEM][PENDING][EXC] {e}")

def learn_from_result(strategy_id: Optional[str], winner: int):
    try:
        if not strategy_id or winner not in (1,2,3,4): return
        pend = MEM["pending"].pop(strategy_id, None)
        if not pend:
            # sem pendente: ainda assim aprende por estratégia “geral”
            key = key_for(strategy_id, [])
        else:
            key = key_for(strategy_id, pend["offered"])

        bucket = MEM["freq"].setdefault(key, {"total":0, "counts":{"1":0,"2":0,"3":0,"4":0}})
        bucket["total"] += 1
        bucket["counts"][str(winner)] += 1
        save_memory(CONFIG["MEMORY_PATH"], MEM)
        print(f"[LEARN] key={key} winner={winner} total={bucket['total']}")
    except Exception as e:
        print(f"[MEM][LEARN][EXC] {e}")

def choose_number(strategy_id: Optional[str], offered: List[int]) -> Tuple[int, float, int]:
    offered = norm_offered(offered)
    key_exact = key_for(strategy_id, offered)
    key_fallback = key_for(strategy_id, [])
    cand_keys = [key_exact, key_fallback]

    best_num, best_score, base_total = None, -1.0, 0
    for k in cand_keys:
        b = MEM["freq"].get(k)
        if not b: continue
        total = b.get("total", 0)
        if total <= 0: continue
        # avalia só entre os oferecidos
        for n in offered:
            cnt = b["counts"].get(str(n), 0)
            score = wilson_p(cnt, total, z=1.96)
            if score > best_score:
                best_num, best_score, base_total = n, score, total

    # caso não tenha histórico, manda mesmo assim: pega o primeiro oferecido
    if best_num is None:
        best_num, best_score, base_total = offered[0], 0.0, 0

    return best_num, best_score, base_total

# =======================
# Envio para Telegram
# =======================
async def tg_send(token: str, chat: str | int, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=10)) as c:
        r = await c.post(url, json=data)
        if r.status_code != 200 or not r.json().get("ok", False):
            print(f"[SEND][ERR] status={r.status_code} body={r.text}")
        else:
            print(f"[SEND][OK] -> {chat}")

# =======================
# FastAPI
# =======================
app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "webhook": "/webhook/<TOKEN>", "status": "/status"}

@app.get("/status")
def status():
    return {
        "ok": True,
        "mem_keys": len(MEM.get("freq", {})),
        "pending": list(MEM.get("pending", {}).keys()),
        "target": CONFIG["TARGET_CHANNEL"],
        "source": CONFIG["PUBLIC_CHANNEL"],
        "mode": CONFIG["SUGGEST_MODE"]
    }

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != CONFIG["WEBHOOK_TOKEN"]:
        return {"ok": False, "err": "bad token"}

    body = await request.json()
    # Telegram pode mandar em channel_post, message, edited_channel_post etc.
    msg = body.get("channel_post") or body.get("message") or body.get("edited_channel_post") or {}
    text: str = (msg.get("text") or msg.get("caption") or "").strip()
    chat = msg.get("chat", {})
    ch_user = chat.get("username")
    msg_id = msg.get("message_id")
    date_ts = msg.get("date")

    print(f"[RX] chat=@{ch_user} id={msg_id} ts={date_ts} text={text[:60]!r}")

    # 1) Sinal com sequência
    offered = extract_offered(text)
    if offered:
        strat = extract_strategy(text)
        # registra pendência para aprender depois quando chegar o GREEN
        remember_offer(strat, offered)

        # escolhe número seco
        num, score, base = choose_number(strat, offered)
        pct = f"{score*100:.2f}%"

        # envia SEMPRE (não pula sinal)
        out = (
            f"🎯 <b>Número seco sugerido:</b> <b>{num}</b>\n"
            f"🧩 Base: estratégia {strat or 'N/D'} | oferta {', '.join(map(str, offered))}\n"
            f"📊 Aprendizado em ~{base} sinais\n"
            f"✅ Chance estimada: {pct}"
        )
        await tg_send(CONFIG["TG_BOT_TOKEN"], CONFIG["TARGET_CHANNEL"], out)
        return {"ok": True, "sent": True}

    # 2) Resultado (GREEN!!! (x)) → aprender
    winner = extract_winner(text)
    if winner:
        strat = extract_strategy(text)
        learn_from_result(strat, winner)
        return {"ok": True, "learned": True, "winner": winner}

    return {"ok": True, "ignored": True}