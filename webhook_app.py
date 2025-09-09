# -*- coding: utf-8 -*-
# Fantan — Número seco imediato + aprendizado persistente (memory.json)
# - Sem variáveis de ambiente: tudo no CONFIG abaixo
# - Parser robusto: KWOK/SSH, ODD/EVEN, SMALL/BIG, "Sequência: …",
#   e "Entrar após o X apostar em …"
# - Envia sugestão SEMPRE (sem filtros)
# - Aprende com GREEN!!! (x) / VENCEDOR: x
# - Endpoints: /status, /ingest, /set_webhook, /webhook/<token>

import json, re, time
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx

# =======================
# CONFIG (edite aqui)
# =======================
CONFIG = {
    # Para ONDE o bot vai ENVIAR a sugestão (canal/grupo/usuário)
    "TARGET_CHANNEL": "@SEU_CANAL_SAIDA",          # ex.: "@fantanvidigal" ou id numérico -100...

    # (Opcional) De ONDE vêm os sinais – só para log/telemetria (não bloqueia)
    "PUBLIC_CHANNEL": "@SEU_CANAL_SINAIS",

    # Token do bot (BotFather)
    "TG_BOT_TOKEN": "8217345207:COLE_SEU_TOKEN_AQUI",

    # Token usado no path do webhook (assinatura simples). Pode ser igual ao TG_BOT_TOKEN.
    "WEBHOOK_TOKEN": "8217345207:COLE_SEU_TOKEN_AQUI",

    # Caminho do “banco” local
    "MEMORY_PATH": "memory.json",

    # Texto “gatilho” comum no sinal (não é obrigatório)
    "ENTRY_MARK": r"ENTRADA\s+CONFIRMADA",

    # SEM RESTRIÇÕES: envia sugestão mesmo sem histórico
    "ALWAYS_SEND": True,

    # Rótulo da mesa (para log)
    "MESA_LABEL": "Fantan - Evolution",
}

# =======================
# Memória persistente
# =======================
def load_memory(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            # freq por chave: "estrategia|nums_ordenados" e "estrategia|" (fallback da estratégia)
            #   {"total": int, "counts": {"1":..,"2":..,"3":..,"4":..}}
            "freq": {},
            # pendências: { "id_estrategia": {"offered":[...], "ts": epoch} }
            "pending": {}
        }

def save_memory(path: str, data: Dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[MEM][ERR] {e}")

MEM = load_memory(CONFIG["MEMORY_PATH"])

# =======================
# Util
# =======================
def norm_offered(nums: List[int]) -> List[int]:
    """1..4, sem duplicados, preservando ordem."""
    seen, out = set(), []
    for n in nums:
        if n in (1,2,3,4) and n not in seen:
            seen.add(n); out.append(n)
    return out

def key_for(strategy_id: Optional[str], offered: List[int]) -> str:
    offered_key = ",".join(map(str, sorted(norm_offered(offered))))
    sid = (strategy_id or "NA").strip()
    return f"{sid}|{offered_key}"

def wilson_upper(count: int, total: int, z: float = 1.96) -> float:
    if total <= 0: return 0.0
    p = count / total
    denom = 1 + (z**2)/total
    num   = p + (z**2)/(2*total)
    rad   = z * ((p*(1-p) + (z**2)/(4*total)) / total) ** 0.5
    upper = (num + rad) / denom
    return max(0.0, min(1.0, upper))

# =======================
# Parsers
# =======================
RE_ENTRY  = re.compile(CONFIG["ENTRY_MARK"], re.I)
RE_STRAT  = re.compile(r"Estrat[eé]gia\s*:\s*([0-9]+)", re.I)

# PT-BR “Entrar após o X apostar em ...” + KWOK/SSH/SMALL/BIG/ODD/EVEN N-N(-N)
RE_PT_ACTION = re.compile(
    r'(?:entrar\s+ap[oó]s\s+o\s*[1-4]\s+apostar\s+em\s+)?'
    r'(kwok|ssh|small|big|odd|even)\s*'
    r'(?:([1-4])\s*(?:[-|/]\s*([1-4]))?(?:\s*[-|/]\s*([1-4]))?)?',
    re.I
)

# genéricos com traço/barra
RE_DASH  = re.compile(r'(?<!\d)([1-4])\s*[-|/]\s*([1-4])(?:\s*[-|/]\s*([1-4]))?')
# com pipes
RE_PIPES = re.compile(r'([1-4])(?:\s*\|\s*([1-4]))(?:\s*\|\s*([1-4]))?')
# “Sequência: 2 | 2 | 4 | 3”
RE_SEQ_LINE = re.compile(r'sequ[eê]ncia[^0-9]*(([1-4][^0-9]+)+[1-4])', re.I)

# atalhos
RE_ODD   = re.compile(r'\bodd\b', re.I)
RE_EVEN  = re.compile(r'\beven\b', re.I)
RE_SMALL = re.compile(r'\bsmall\b', re.I)
RE_BIG   = re.compile(r'\bbig\b', re.I)

# resultado: GREEN!!! (3)  ou  VENCEDOR: 3
RE_GREEN = re.compile(r'GREEN.+?\(([1-4])\)', re.I)
RE_WON   = re.compile(r'VENCEDOR\s*:\s*([1-4])', re.I)

def extract_strategy(text: str) -> Optional[str]:
    m = RE_STRAT.search(text)
    return m.group(1) if m else None

def _uniq_groups(groups) -> List[int]:
    out = []
    for g in groups:
        if not g: continue
        n = int(g)
        if n in (1,2,3,4) and n not in out:
            out.append(n)
    return out

def extract_candidates(text: str) -> List[int]:
    t = text

    # atalhos diretos
    if RE_ODD.search(t):   return [1, 3]
    if RE_EVEN.search(t):  return [2, 4]
    if RE_SMALL.search(t): return [1, 2]
    if RE_BIG.search(t):   return [3, 4]

    # “Entrar após o X apostar em <KWOK/SSH/SMALL/BIG/ODD/EVEN> [N-N(-N)]”
    m = RE_PT_ACTION.search(t)
    if m:
        kind = (m.group(1) or "").lower()
        nums = _uniq_groups(m.groups()[1:])
        if kind == "odd":   return [1,3]
        if kind == "even":  return [2,4]
        if kind == "small": return nums if nums else [1,2]
        if kind == "big":   return nums if nums else [3,4]
        if kind in ("kwok","ssh"):
            if nums: return nums

    # genéricos 3-2-1 / 3|2|1 / 3/2/1
    m = RE_DASH.search(t)
    if m: return _uniq_groups(m.groups())
    m = RE_PIPES.search(t)
    if m: return _uniq_groups(m.groups())

    # “Sequência: 2 | 2 | 4 | 3” → pega últimos 2–3 distintos
    m = RE_SEQ_LINE.search(t)
    if m:
        nums = [int(x) for x in re.findall(r'[1-4]', m.group(1))]
        if nums:
            out = []
            for n in reversed(nums):
                if n not in out:
                    out.append(n)
                if len(out) >= 3:
                    break
            return list(reversed(out))

    return []

def extract_winner(text: str) -> Optional[int]:
    m = RE_GREEN.search(text)
    if m: return int(m.group(1))
    m = RE_WON.search(text)
    if m: return int(m.group(1))
    return None

# =======================
# Aprendizado
# =======================
def remember_offer(strategy_id: Optional[str], offered: List[int]):
    """Registra pendência por estratégia (se houver)."""
    try:
        if not offered: return
        if strategy_id:
            MEM["pending"][strategy_id] = {"offered": norm_offered(offered), "ts": time.time()}
            save_memory(CONFIG["MEMORY_PATH"], MEM)
    except Exception as e:
        print(f"[MEM][PENDING][ERR] {e}")

def learn_from_result(strategy_id: Optional[str], winner: int):
    """Fecha pendência e acumula frequência por (estratégia|oferta) e (estratégia|)."""
    try:
        if not strategy_id or winner not in (1,2,3,4): return
        pend = MEM["pending"].pop(strategy_id, None)
        offered = pend["offered"] if pend else []

        # chave exata (estratégia + candidatos ordenados)
        key_exact = key_for(strategy_id, offered)
        buck = MEM["freq"].setdefault(key_exact, {"total":0,"counts":{"1":0,"2":0,"3":0,"4":0}})
        buck["total"] += 1
        buck["counts"][str(winner)] += 1

        # fallback por estratégia apenas (sem candidatos)
        key_fallback = key_for(strategy_id, [])
        buck2 = MEM["freq"].setdefault(key_fallback, {"total":0,"counts":{"1":0,"2":0,"3":0,"4":0}})
        buck2["total"] += 1
        buck2["counts"][str(winner)] += 1

        save_memory(CONFIG["MEMORY_PATH"], MEM)
        print(f"[LEARN] key={key_exact} winner={winner} total={buck['total']}")
    except Exception as e:
        print(f"[MEM][LEARN][ERR] {e}")

def choose_number(strategy_id: Optional[str], offered: List[int]) -> (int, float, int, str):
    """
    Retorna (numero_escolhido, prob_estimada, base_total, key_usada)
    Usa Wilson (upper) por segurança; se não houver histórico, escolhe o primeiro.
    """
    offered = norm_offered(offered)
    if not offered:
        return 1, 0.0, 0, "fallback"

    key_exact = key_for(strategy_id, offered)
    key_fallback = key_for(strategy_id, [])
    cand_keys = [key_exact, key_fallback]

    best_num, best_score, base_total, used_key = None, -1.0, 0, ""
    for k in cand_keys:
        b = MEM["freq"].get(k)
        if not b: continue
        total = b.get("total", 0)
        if total <= 0: continue
        for n in offered:
            cnt = b["counts"].get(str(n), 0)
            score = wilson_upper(cnt, total, 1.96)  # estimação conservadora
            if score > best_score:
                best_num, best_score, base_total, used_key = n, score, total, k

    if best_num is None:
        # sem histórico → escolha determinística = primeiro dos oferecidos
        return offered[0], 0.0, 0, "no-history"

    return best_num, best_score, base_total, used_key

# =======================
# Envio Telegram
# =======================
async def tg_send(text: str):
    url = f"https://api.telegram.org/bot{CONFIG['TG_BOT_TOKEN']}/sendMessage"
    data = {"chat_id": CONFIG["TARGET_CHANNEL"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10)) as c:
        r = await c.post(url, json=data)
        if r.status_code != 200 or not r.json().get("ok", False):
            print(f"[SEND][ERR] status={r.status_code} body={r.text}")
        else:
            print(f"[SEND][OK] -> {CONFIG['TARGET_CHANNEL']}")

# =======================
# FastAPI
# =======================
app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "webhook": "/webhook/<token>", "status": "/status", "set_hook": "/set_webhook"}

@app.get("/status")
def status():
    return {
        "ok": True,
        "freq_keys": len(MEM.get("freq", {})),
        "pending": list(MEM.get("pending", {}).keys()),
        "target": CONFIG["TARGET_CHANNEL"],
        "source": CONFIG["PUBLIC_CHANNEL"],
        "entry_mark": CONFIG["ENTRY_MARK"],
        "mesa": CONFIG["MESA_LABEL"],
    }

@app.post("/ingest")
async def ingest(payload: Dict[str, Any]):
    """
    Ingestão retroativa simples:
    payload = {"items":[{"text":"...mensagem..."}, ...]}
    """
    items = payload.get("items", [])
    added_offer = added_win = 0
    for it in items:
        t = (it.get("text") or "").strip()
        if not t: continue
        offered = extract_candidates(t)
        if offered:
            strat = extract_strategy(t)
            remember_offer(strat, offered)
            # também podemos já escolher, mas ingestão não envia sinais
            added_offer += 1
        w = extract_winner(t)
        if w:
            strat = extract_strategy(t)
            learn_from_result(strat, w)
            added_win += 1
    return {"ok": True, "offers": added_offer, "winners": added_win}

@app.get("/set_webhook")
async def set_webhook():
    """
    Atalho para registrar o webhook no Telegram.
    Chame: /set_webhook?base=https://SEUAPP.onrender.com
    """
    base = ""  # ex.: https://seuapp.onrender.com
    from fastapi import Query
    # FastAPI não aceita Query aqui sem assin., então parse manual do status endpoint:
    # Use via navegador: /set_webhook?base=https://SEUAPP.onrender.com
    return {"ok": False, "hint": "Use: https://api.telegram.org/bot<TOKEN>/setWebhook?url=<base>/webhook/<WEBHOOK_TOKEN>"}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != CONFIG["WEBHOOK_TOKEN"]:
        raise HTTPException(status_code=403, detail="bad token")

    body = await request.json()
    msg = body.get("channel_post") or body.get("message") or body.get("edited_channel_post") or {}
    text: str = (msg.get("text") or msg.get("caption") or "").strip()
    chat = msg.get("chat", {})
    username = (chat.get("username") or "").lower()
    msg_id = msg.get("message_id")
    date_ts = msg.get("date")

    print(f"[RX] @{username} id={msg_id} ts={date_ts} text={text[:80]!r}")

    # (Opcional) filtrar por origem informativa (não bloqueia se diferente)
    if CONFIG["PUBLIC_CHANNEL"]:
        src = CONFIG["PUBLIC_CHANNEL"].lstrip("@").lower()
        if username and username != src:
            print(f"[RX] origem diferente de {CONFIG['PUBLIC_CHANNEL']}, mas não bloqueado.")

    # 1) SINAL → extrai candidatos e envia sugestão na hora
    offered = extract_candidates(text)
    if offered or RE_ENTRY.search(text or ""):
        # se não extraiu nada mas tem ENTRY_MARK, tenta fallback 1 número do texto
        if not offered:
            # pega o primeiro número que aparecer no texto como fallback extremo
            m = re.search(r'\b([1-4])\b', text)
            offered = [int(m.group(1))] if m else [1]

        strat = extract_strategy(text)
        remember_offer(strat, offered)

        num, p, base, used_key = choose_number(strat, offered)
        pct = f"{p*100:.2f}%"
        out = (
            f"🎯 <b>Número seco sugerido:</b> <b>{num}</b>\n"
            f"🧩 Base: estratégia <code>{strat or 'N/D'}</code> | oferta <code>{', '.join(map(str, offered))}</code>\n"
            f"📊 Aprendizado em ~<b>{base}</b> sinais (chave: <code>{used_key}</code>)\n"
            f"✅ Chance estimada: <b>{pct}</b>"
        )
        await tg_send(out)
        return JSONResponse({"ok": True, "sent": True})

    # 2) RESULTADO → aprender
    winner = extract_winner(text)
    if winner:
        strat = extract_strategy(text)
        learn_from_result(strat, winner)
        return JSONResponse({"ok": True, "learned": True, "winner": winner})

    return JSONResponse({"ok": True, "ignored": True})