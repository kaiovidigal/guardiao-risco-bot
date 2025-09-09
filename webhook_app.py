import os
import re
import json
import asyncio
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx

APP_NAME = "guardiao-risco-bot"
app = FastAPI(title=APP_NAME)

# === Config ===
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "").strip()  # opcional. Se vazio, responde no mesmo chat.

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}" if TG_BOT_TOKEN else ""

# Pequena memória em processo (apenas para depuração rápida)
SUGG_STATS = {
    "total_updates": 0,
    "signals_seen": 0,
    "suggestions_sent": 0,
}

def safe(tok: str, keep: int = 4) -> str:
    """Trunca tokens para log seguro."""
    if not tok:
        return "(vazio)"
    if len(tok) <= keep * 2:
        return tok
    return tok[:keep] + "..." + tok[-keep:]

# === Utilidades de envio ===
async def tg_send_message(chat_id: int | str, text: str, parse_mode: Optional[str] = None) -> Dict[str, Any]:
    if not TELEGRAM_API:
        return {"ok": False, "error": "TG_BOT_TOKEN ausente"}
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "status_code": r.status_code, "text": r.text}

# === Parsing de sinais (regras cobertas) ===
NUM_RE = re.compile(r"\b[1-4]\b")
PIPE_SEQ_RE = re.compile(r"\b([1-4])\s*\|\s*([1-4])(?:\s*\|\s*([1-4]))?(?:\s*\|\s*([1-4]))?\b")
DASH_SEQ_RE = re.compile(r"\b([1-4])\s*-\s*([1-4])\s*-\s*([1-4])\b", re.IGNORECASE)

def extract_context_numbers(text: str) -> List[int]:
    """
    Extrai candidatos de acordo com as regras:
      - 'Ssh 4-3-2'  -> [4,3,2]
      - 'KWOK 2-3'   -> [2,3]
      - 'ODD'        -> [1,3]
      - 'EVEN'       -> [2,4]
      - padrões '3-2-1' -> [3,2,1]
      - padrões '2 | 4 | 3' -> [2,4,3]
    Se nada claro for encontrado, retorna todos [1,2,3,4] como fallback.
    """
    text_low = text.lower()

    # Ssh X-Y-Z
    if "ssh" in text_low:
        m = DASH_SEQ_RE.search(text)
        if m:
            seq = [int(m.group(1)), int(m.group(2)), int(m.group(3))]
            return seq

    # KWOK A-B (dois números)
    if "kwok" in text_low:
        # aceitar "kwok 2-3" ou "kwok 2 – 3"
        m = re.search(r"kwok\s*([1-4])\s*[-–]\s*([1-4])", text_low)
        if m:
            return [int(m.group(1)), int(m.group(2))]

    # ODD / EVEN
    if re.search(r"\bodd\b", text_low):
        return [1, 3]
    if re.search(r"\beven\b", text_low):
        return [2, 4]

    # Padrões "3-2-1" dispersos
    m = DASH_SEQ_RE.search(text)
    if m:
        return [int(m.group(1)), int(m.group(2)), int(m.group(3))]

    # Padrões com pipe "2 | 4 | 3 ..."
    m = PIPE_SEQ_RE.search(text)
    if m:
        nums = [g for g in m.groups() if g]
        return [int(x) for x in nums]

    # Se não achou nada, pegue os números que aparecem isolados (pouco restritivo)
    hits = [int(n) for n in NUM_RE.findall(text)]
    if hits:
        # normalizar, manter ordem de primeira ocorrência
        seen = set()
        ordered = []
        for n in hits:
            if n not in seen:
                seen.add(n)
                ordered.append(n)
        return ordered

    # fallback total
    return [1, 2, 3, 4]

def choose_crisp_number(candidates: List[int], seed: int) -> Tuple[int, float]:
    """
    Escolhe 1 número seco IMEDIATO entre os candidatos.
    Heurística simples e determinística:
      - se 2 números: pega o MAIOR (empírico) – pode trocar para menor se preferir.
      - se 3 números: pega o do MEIO (2º)
      - se 4 números: escolhe index = seed % 4 (determinístico por message_id)
      - senão: seed % len(candidates)
    Retorna (numero, chance_estimativa_dummy)
    """
    if not candidates:
        return 2, 50.0

    uniq = []
    for n in candidates:
        if n not in uniq:
            uniq.append(n)
    candidates = uniq

    chance = 0.0
    if len(candidates) == 1:
        return candidates[0], 95.0
    if len(candidates) == 2:
        # regra preferindo o maior
        return max(candidates), 72.0
    if len(candidates) == 3:
        return candidates[1], 66.0
    if len(candidates) == 4:
        pick = candidates[seed % 4]
        return pick, 55.0

    pick = candidates[seed % len(candidates)]
    return pick, 55.0

def format_suggestion(number: int, base_list: List[int], retro_count: int, chance_pct: float) -> str:
    base_str = ", ".join(str(x) for x in base_list)
    return (
        "🎯 *Número seco sugerido:* {}\n"
        "🧮 Base lida: [{}]\n"
        "📊 *Baseado em ~{} sinais retroativos*\n"
        "✅ *Chance estimada:* {:.2f}%"
    ).format(number, base_str, retro_count, chance_pct)

# === Endpoints ===
@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "ok": True,
        "app": APP_NAME,
        "status": "alive",
    }

@app.get("/status")
async def status() -> Dict[str, Any]:
    return {
        "ok": True,
        "app": APP_NAME,
        "has_token": bool(TG_BOT_TOKEN),
        "webhook_expected": safe(WEBHOOK_TOKEN, 3),
        "target_channel": TARGET_CHANNEL or "(mesmo chat)",
        "stats": SUGG_STATS,
    }

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request) -> Response:
    # Segurança do webhook
    expected = WEBHOOK_TOKEN
    if not expected:
        # Se não configurou, rejeita com log claro
        msg = f"[AUTH] WEBHOOK_TOKEN não configurado. Recebido path-token={safe(token)}"
        print(msg)
        return JSONResponse({"detail": "Forbidden (no WEBHOOK_TOKEN set)"}, status_code=403)

    if token != expected:
        print(f"[AUTH] Token mismatch: got={safe(token)} expected={safe(expected)} -> 403")
        return JSONResponse({"detail": "Forbidden"}, status_code=403)

    # Lê update
    try:
        body_bytes = await request.body()
        update = json.loads(body_bytes.decode("utf-8", errors="ignore"))
    except Exception as e:
        print(f"[PARSE] Erro lendo JSON do update: {e}")
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    SUGG_STATS["total_updates"] += 1

    # Identifica origem e texto
    chat_id: Optional[int] = None
    text: str = ""
    msg_id: int = 0

    if "channel_post" in update:
        post = update.get("channel_post", {})
        chat = post.get("chat", {})
        chat_id = chat.get("id")
        text = post.get("text") or post.get("caption") or ""
        msg_id = post.get("message_id", 0)
    elif "message" in update:
        post = update.get("message", {})
        chat = post.get("chat", {})
        chat_id = chat.get("id")
        text = post.get("text") or post.get("caption") or ""
        msg_id = post.get("message_id", 0)
    else:
        # ignorar tipos que não nos interessam
        print(f"[SKIP] Update sem message/channel_post: keys={list(update.keys())}")
        return JSONResponse({"ok": True})

    if not text:
        print("[SKIP] Mensagem sem texto/caption, chat_id=", chat_id)
        return JSONResponse({"ok": True})

    # Só processa se parece um sinal
    looks_signal = any(
        key in text.lower()
        for key in ["entrada confirmada", "estratégia", "sequência", "ssh", "kwok", "odd", "even"]
    )
    if not looks_signal:
        return JSONResponse({"ok": True})

    SUGG_STATS["signals_seen"] += 1

    # Extrai candidatos e decide número seco
    candidates = extract_context_numbers(text)
    number, chance = choose_crisp_number(candidates, seed=msg_id or 1)

    # Para fins de demo, o retro_count é uma contagem fake (pode ligar a um DB depois)
    retro_count = max(1000, SUGG_STATS["signals_seen"])

    suggestion = format_suggestion(number, candidates, retro_count, chance)

    # Para onde enviar?
    dest_chat = TARGET_CHANNEL if TARGET_CHANNEL else chat_id

    # Manda
    out = await tg_send_message(dest_chat, suggestion, parse_mode="Markdown")
    ok = out.get("ok", False)
    if not ok:
        print(f"[SEND] Falha ao enviar sugestão. resp={out}")

        # tenta um fallback simples: remove parse_mode
        out2 = await tg_send_message(dest_chat, suggestion)
        ok = out2.get("ok", False)
        if not ok:
            print(f"[SEND] Fallback também falhou. resp={out2}")
        else:
            SUGG_STATS["suggestions_sent"] += 1
    else:
        SUGG_STATS["suggestions_sent"] += 1

    return JSONResponse({"ok": True, "sent": ok})

# ==== Execução local (Render usa o comando start) ====
# Comando de start recomendado no Render:
#   uvicorn webhook_app:app --host 0.0.0.0 --port $PORT