# app.py
# FastAPI + Webhook do Telegram (processa só "ANALISANDO" / "ENTRADA CONFIRMADA")
# Lê do canal-fonte (opcional) e publica no canal-destino com uma formatação simples.

import os
import re
import json
from collections import deque, Counter

import httpx
from fastapi import FastAPI, Request, HTTPException, Query

# ========= ENV =========
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()                 # ex: 8315:AAA...
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "meusegredo123").strip()   # ex: meusegredo123
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "").strip()               # ex: -1002796105884  (OBRIGATÓRIO)
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()               # ex: -1002810508717  (opcional, filtra)
DEBUG          = os.getenv("DEBUG_MSG", "0").strip() in ("1","true","True","yes","YES")

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not TARGET_CHANNEL:
    raise RuntimeError("Defina TARGET_CHANNEL no ambiente.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ========= App =========
app = FastAPI(title="telegram-webhook-min", version="1.0.0")

# ========= Regex / Parsing =========
ENTRY_RX       = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
ANALISANDO_RX  = re.compile(r"\bANALISANDO\b", re.I)
SEQ_RX         = re.compile(r"Sequ[eê]ncia:\s*([^\n\r]+)", re.I)
ANY_14_RX      = re.compile(r"[1-4]")

def extract_seq(text: str):
    """Extrai uma lista de números 1..4 da linha 'Sequência:' (se existir)."""
    if not text:
        return []
    m = SEQ_RX.search(text)
    if not m:
        return []
    return [int(x) for x in ANY_14_RX.findall(m.group(1))][:10]  # até 10 por segurança

# ======= Dedupe simples por update_id =======
_seen_ids = deque(maxlen=1000)
_seen_set = set()

def seen(update_id: str) -> bool:
    if not update_id:
        return False
    if update_id in _seen_set:
        return True
    _seen_ids.append(update_id)
    _seen_set.add(update_id)
    # mantém o set em sincronia com a deque
    if len(_seen_set) > _seen_ids.maxlen:
        while len(_seen_set) > _seen_ids.maxlen:
            _seen_set.discard(_seen_ids.popleft())
    return False

# ========= Telegram helpers =========
async def tg_send_text(chat_id: str, text: str, parse: str = "HTML"):
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.post(f"{TELEGRAM_API}/sendMessage",
                           json={"chat_id": chat_id,
                                 "text": text,
                                 "parse_mode": parse,
                                 "disable_web_page_preview": True})
        r.raise_for_status()

# ========= Health / Root / Set Webhook =========
@app.get("/")
async def root():
    return {"ok": True, "service": app.title, "version": app.version}

@app.get("/health")
async def health():
    return {
        "ok": True,
        "has_token": bool(TG_BOT_TOKEN),
        "webhook_token_set": bool(WEBHOOK_TOKEN),
        "source_filter": SOURCE_CHANNEL or "(any)",
        "target": TARGET_CHANNEL,
    }

@app.get("/set_webhook")
async def set_webhook(host: str = Query(..., description="Ex.: https://guardiao-risco-bot-2.onrender.com")):
    """Helper: seta o webhook no Telegram com o token correto."""
    url = f"{host.rstrip('/')}/webhook/{WEBHOOK_TOKEN}"
    async with httpx.AsyncClient(timeout=15) as cli:
        r = await cli.get(f"{TELEGRAM_API}/setWebhook", params={"url": url})
        data = r.json()
    return {"requested_url": url, "telegram_response": data}

# ========= Webhook =========
@app.post(f"/webhook/{WEBHOOK_TOKEN}")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Dedupe por update_id (se vier)
    upd_id = str(update.get("update_id", "") or "")
    if seen(upd_id):
        return {"ok": True, "skipped": "duplicate"}

    # Pega mensagem de canal ou de chat
    msg = update.get("channel_post") or update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    # Filtra por canal-fonte, se configurado
    if SOURCE_CHANNEL and chat_id != SOURCE_CHANNEL:
        if DEBUG:
            await tg_send_text(TARGET_CHANNEL, f"DEBUG: ignorando chat {chat_id}, esperado {SOURCE_CHANNEL}")
        return {"ok": True, "skipped": "other_chat"}

    # Extrai texto/caption
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return {"ok": True, "skipped": "no_text"}

    # ====== LÓGICA DE PROCESSAMENTO ======
    # 1) ANALISANDO: só registra e pode emitir um preview
    if ANALISANDO_RX.search(text):
        seq = extract_seq(text)
        preview = f"🔎 <b>ANALISANDO</b>\n📊 Sequência detectada: {seq or '—'}"
        await tg_send_text(TARGET_CHANNEL, preview)
        return {"ok": True, "analise_seq": seq}

    # 2) ENTRADA CONFIRMADA: decide um número simples (demo) e publica
    if ENTRY_RX.search(text):
        seq = extract_seq(text)

        # === heurística simples de exemplo ===
        # pega o número mais frequente na sequência informada;
        # se empatar, usa o último número da sequência
        best = None
        if seq:
            c = Counter(seq)
            top = c.most_common()
            if len(top) >= 2 and top[0][1] == top[1][1]:
                best = seq[-1]
            else:
                best = top[0][0]

        # Mensagem final
        if best is not None:
            out = (
                f"🤖 <b>IA SUGERE</b> — <b>{best}</b>\n"
                f"🧩 <b>Base:</b> ENTRADA CONFIRMADA\n"
                f"📈 <b>Sequência:</b> {seq or '—'}"
            )
        else:
            out = "🤖 <b>IA SUGERE</b> — não foi possível decidir (sem sequência)."

        await tg_send_text(TARGET_CHANNEL, out)
        return {"ok": True, "posted": True, "best": best}

    # Sem regra aplicada: ignore silenciosamente
    if DEBUG:
        await tg_send_text(TARGET_CHANNEL, "DEBUG: mensagem ignorada (sem regra).")
    return {"ok": True, "skipped": "no_rule"}