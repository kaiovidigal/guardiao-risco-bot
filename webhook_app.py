# webhook_app.py
# FastAPI + Telegram webhook
# - Lê TG_BOT_TOKEN, WEBHOOK_TOKEN, PUBLIC_CHANNEL do ambiente
# - POST /webhook/<WEBHOOK_TOKEN> (rota fixa) e /webhook/{token} (dinâmica)
# - Espelha "ENTRADA CONFIRMADA" do canal-fonte e acompanha GREEN/LOSS até G2

import os, re
from typing import Optional, List
from fastapi import FastAPI, Request, HTTPException
import httpx

# ====== ENV ======
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "meusegredo123").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()   # ex: -1002796105884

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not PUBLIC_CHANNEL:
    raise RuntimeError("Defina PUBLIC_CHANNEL no ambiente.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ====== APP ======
app = FastAPI(title="Guardiao Webhook (G0/G1/G2 visível)")

# ====== Parsers ======
ENTRY_RX = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
GREEN_RX = re.compile(r"(?:\bgreen\b|\bwin\b|✅)", re.I)
RED_RX   = re.compile(r"(?:\bred\b|\bloss\b|❌|\bperdemos\b)", re.I)
PAREN_GROUP_RX = re.compile(r"\(([^)]*)\)")
ANY_14_RX      = re.compile(r"[1-4]")

def is_entry_confirmed(text: str) -> bool:
    """Aceita qualquer variação contendo 'ENTRADA CONFIRMADA' (tolerante)."""
    return bool(ENTRY_RX.search(re.sub(r"\s+", " ", text)))

def parse_close_numbers(text: str) -> List[int]:
    """Extrai até 3 números do fechamento (entre parênteses ou soltos)."""
    t = re.sub(r"\s+", " ", text)
    groups = PAREN_GROUP_RX.findall(t)
    if groups:
        last = groups[-1]
        nums = re.findall(r"[1-4]", last)
        return [int(x) for x in nums][:3]
    nums = ANY_14_RX.findall(t)
    return [int(x) for x in nums][:3]

def extract_sequence_bases(text: str) -> List[int]:
    """Pega os primeiros números após 'Sequência:'; se não houver, vazio."""
    m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", text, flags=re.I)
    if not m:
        return []
    nums = re.findall(r"[1-4]", m.group(1))
    base: List[int] = []
    for n in nums:
        i = int(n)
        if not base or base[-1] != i:
            base.append(i)
    return base[:3]

def extract_after(text: str) -> Optional[int]:
    m = re.search(r"ap[oó]s\s+o\s+([1-4])", text, flags=re.I)
    return int(m.group(1)) if m else None

# ====== Telegram helpers ======
async def tg_send_text(chat_id: str, text: str, parse: str = "HTML"):
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True},
        )

# ====== Estado simples em memória (uma pendência por vez) ======
_pending = {
    "open": False,
    "suggested": None,     # apenas informativo
    "seen": [],            # números observados do fonte (ex.: [1,3,3])
}

def _reset_pending():
    _pending["open"] = False
    _pending["suggested"] = None
    _pending["seen"] = []

# ====== Rotas (health + webhook) ======
@app.get("/")
async def health():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>", "pending_open": _pending["open"]}

# Rota dinâmica: aceita /webhook/QUALQUER e valida o token
@app.post("/webhook/{token}")
async def webhook_dyn(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return await _handle_update(request)

# Rota fixa: /webhook/<WEBHOOK_TOKEN> (evita 404 mesmo com setWebhook direto)
@app.post(f"/webhook/{WEBHOOK_TOKEN}")
async def webhook_fixed(request: Request):
    return await _handle_update(request)

# ====== Núcleo do processamento ======
async def _handle_update(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    msg = data.get("channel_post") or data.get("message") or data.get("edited_channel_post") or data.get("edited_message") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if not text:
        return {"ok": True, "skipped": "sem_texto"}

    # 1) Fechamentos (GREEN/RED)
    if GREEN_RX.search(text) or RED_RX.search(text):
        if not _pending["open"]:
            return {"ok": True, "noted": "fechamento_sem_pendente"}

        nums = parse_close_numbers(text)
        if nums:
            for n in nums:
                if len(_pending["seen"]) < 3:
                    _pending["seen"].append(int(n))

        # Quando tiver 3 observados, fecha
        if len(_pending["seen"]) >= 3:
            seen_txt = "-".join(str(x) for x in _pending["seen"][:3])
            sug = _pending["suggested"]
            outcome = "GREEN" if sug in _pending["seen"][:3] else "LOSS"
            # Estágio apenas informativo
            stage = "G0" if (len(_pending["seen"]) == 1 and outcome == "GREEN") \
                else ("G1" if (len(_pending["seen"]) == 2 and outcome == "GREEN") \
                else ("G2" if outcome == "GREEN" else "G2"))

            our = sug if outcome == "GREEN" else "X"
            icon = "🟢" if outcome == "GREEN" else "🔴"
            out = f"{icon} <b>{outcome}</b> — finalizado (<b>{stage}</b>, nosso={our}, observados={seen_txt})."
            await tg_send_text(PUBLIC_CHANNEL, out)
            _reset_pending()
        return {"ok": True, "closed_maybe": True}

    # 2) Entrada confirmada -> abre pendência e publica G0 visível
    if is_entry_confirmed(text):
        # Se havia 2 observados e o fonte começou novo sinal, fecha com X
        if _pending["open"] and len(_pending["seen"]) == 2:
            seen_txt = "-".join([str(x) for x in _pending["seen"]] + ["X"])
            sug = _pending["suggested"]
            outcome = "GREEN" if sug in _pending["seen"] else "LOSS"
            stage = "G1" if outcome == "GREEN" else "G2"
            our = sug if outcome == "GREEN" else "X"
            icon = "🟢" if outcome == "GREEN" else "🔴"
            await tg_send_text(PUBLIC_CHANNEL, f"{icon} <b>{outcome}</b> — finalizado (<b>{stage}</b>, nosso={our}, observados={seen_txt}).")
            _reset_pending()

        bases = extract_sequence_bases(text)        # ex.: [1,3,3]
        after = extract_after(text)                 # ex.: 1
        # escolha simples e determinística para G0:
        suggested = bases[0] if bases else (after if after else 1)

        _pending["open"] = True
        _pending["suggested"] = suggested
        _pending["seen"] = []

        aft_txt = f" após {after}" if after else ""
        base_txt = ", ".join(str(x) for x in bases) if bases else "—"
        msg_out = (
            f"🎯 <b>Número seco (G0):</b> <b>{suggested}</b>\n"
            f"🧩 <b>Padrão:</b> GEN{aft_txt}\n"
            f"🧮 <b>Base:</b> [{base_txt}]"
        )
        await tg_send_text(PUBLIC_CHANNEL, msg_out)
        return {"ok": True, "opened": True, "suggested": suggested}

    # 3) Não é entrada/fechamento (ignora)
    return {"ok": True, "skipped": True}