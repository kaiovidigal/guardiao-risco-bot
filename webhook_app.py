# webhook_app.py
import os
import json
import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from pydantic import BaseModel
import uvicorn

# --- logging bonitinho ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("fantan-auto")

app = FastAPI(title="guardiao-risco-bot webhook")

# ========================
# Config por variáveis de ambiente
# ========================
CONF_MIN      = float(os.getenv("CONF_MIN", "0.82"))
MIN_SUP_G0    = int(os.getenv("MIN_SUP_G0", "10"))
MIN_SUP_G1    = int(os.getenv("MIN_SUP_G1", "8"))
N_MAX         = int(os.getenv("N_MAX", "4"))
Z_WILSON      = float(os.getenv("Z_WILSON", "1.96"))
COOLDOWN_S    = int(os.getenv("COOLDOWN_S", "6"))
SEND_SIGNALS  = int(os.getenv("SEND_SIGNALS", "1"))
REPORT_EVERY  = int(os.getenv("REPORT_EVERY", "5"))
RESULTS_WINDOW= int(os.getenv("RESULTS_WINDOW", "30"))

PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()    # ex: @fantanovidigal
AUTO_SCRAPE    = int(os.getenv("AUTO_SCRAPE", "1"))
SCRAPE_EVERY_S = int(os.getenv("SCRAPE_EVERY_S", "120"))
SCRAPE_LIMIT   = int(os.getenv("SCRAPE_LIMIT", "1000"))

# Para mostrar no /status
def current_conf() -> Dict[str, Any]:
    return {
        "CONF_MIN": CONF_MIN,
        "MIN_SUP_G0": MIN_SUP_G0,
        "MIN_SUP_G1": MIN_SUP_G1,
        "N_MAX": N_MAX,
        "Z_WILSON": Z_WILSON,
        "COOLDOWN_S": COOLDOWN_S,
        "SEND_SIGNALS": SEND_SIGNALS,
        "REPORT_EVERY": REPORT_EVERY,
        "RESULTS_WINDOW": RESULTS_WINDOW,
    }

def current_scrape() -> Dict[str, Any]:
    return {
        "PUBLIC_CHANNEL": PUBLIC_CHANNEL,
        "SCRAPE_EVERY_S": SCRAPE_EVERY_S,
        "SCRAPE_LIMIT": SCRAPE_LIMIT,
        "AUTO_SCRAPE": AUTO_SCRAPE,
    }

# ========================
# Model para testes do /ingest
# ========================
class IngestPayload(BaseModel):
    chat_id: Optional[int] = None
    message_id: Optional[int] = None
    text: str

# ========================
# Endpoints
# ========================

@app.get("/")
async def root():
    return {"ok": True, "hint": "use /status, /ingest (POST) ou /webhook/{token}"}

@app.get("/status")
async def status():
    return {
        "ok": True,
        "conf": current_conf(),
        "scrape": current_scrape(),
        "bot": {"enabled": bool(os.getenv("TG_BOT_TOKEN", "").strip())},
        "webhook": "/webhook/{token}",
    }

@app.post("/ingest")
async def ingest(payload: IngestPayload):
    """
    Endpoint de teste manual: você pode postar textos aqui
    para simular um 'sinal' e ver o processamento.
    """
    log.info("🧪 /ingest recebido: %s", payload.model_dump())
    # aqui você poderia chamar o mesmo pipeline de parse/score que o scraper usa
    return {"ok": True, "echo": payload.model_dump()}

# ------------ IMPORTANTE -------------
# Webhook dinâmico: aceita /webhook/<token> (o formato que o Telegram usa)
@app.post("/webhook/{token}")
async def webhook_dyn(token: str, request: Request):
    try:
        data = await request.json()
    except Exception:
        # fallback: pode vir como form/urlencoded em alguns casos
        body = await request.body()
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = {"raw": body.decode("utf-8", errors="ignore")}
    log.info("📩 Webhook recebido para token=%s: %s", token, json.dumps(data)[:2000])
    # Se quiser validar o token, compare com TG_BOT_TOKEN do ambiente
    tg_token = os.getenv("TG_BOT_TOKEN", "").strip()
    if tg_token and token != tg_token:
        log.warning("⚠️ Token no path não confere com TG_BOT_TOKEN do ambiente.")
    # TODO: aqui você processa updates do Telegram (mensagens, callbacks etc.)
    return {"ok": True}

# Webhook fixo (compat): aceita /webhook sem token e encaminha para o dinâmico
@app.post("/webhook")
async def webhook_fixed(request: Request):
    token = os.getenv("TG_BOT_TOKEN", "no-token")
    return await webhook_dyn(token, request)

# ========================
# Eventos de inicialização
# ========================
@app.on_event("startup")
async def on_startup():
    # só logs úteis; todo o scraper/Telethon roda no outro módulo/worker
    log.info("ℹ️  Inicialização do aplicativo concluída.")
    # Dica de webhook registrado (se aplicável)
    base = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    tg_token = os.getenv("TG_BOT_TOKEN", "").strip()
    if base and tg_token:
        log.info("🔗 Webhook esperado em %s/webhook/%s", base, tg_token)
    else:
        log.info("🔗 Você pode chamar /webhook/{token} diretamente sem registrar no Telegram.")

# ========================
# Uvicorn local (Render ignora se usa 'Start Command')
# ========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("webhook_app:app", host="0.0.0.0", port=port, reload=False)
