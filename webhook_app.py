# -*- coding: utf-8 -*-
# Fan Tan — Guardião (versão corrigida)
# Inclui:
# - Fix para GET/HEAD do webhook (não retorna mais 404)
# - Link correto para setWebhook com token
# - Conferência estrita de GREEN/RED (só fecha se número estiver presente e coerente)

import os, re, json, time, sqlite3, asyncio, shutil
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime, timezone, timedelta
from collections import Counter

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# ENV / CONFIG
# =========================
DB_PATH = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"

TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "8217345207:AAEf5DjyRgIzxtDlTZVJX5bOjLw-uSg_i5o").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "meusegredo123").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI(title="Fantan Guardião — FIRE-only (G0 + Recuperação oculta)", version="3.10.0")

# =========================
# Utils / Telegram
# =========================
def now_ts() -> int:
    return int(time.time())

async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True},
        )

async def tg_broadcast(text: str, parse: str="HTML"):
    if PUBLIC_CHANNEL:
        await tg_send_text(PUBLIC_CHANNEL, text, parse)

# =========================
# Parsers (simplificado para exemplo)
# =========================
def _detect_outcome_clear(text: str) -> tuple[Optional[str], Optional[int]]:
    t = re.sub(r"\s+", " ", text)

    has_green = re.search(r"\bGREEN\b", t, flags=re.I) is not None
    has_red   = re.search(r"\b(?:RED|LOSS)\b", t, flags=re.I) is not None
    if not (has_green or has_red):
        return None, None

    n_paren = None
    m = re.search(r"\b(?:GREEN|RED|LOSS)\b.*?\(([^\)]*)\)", t, flags=re.I)
    if m:
        d = re.search(r"[1-4]", m.group(1))
        if d: n_paren = int(d.group(0))

    n_num = None
    m = re.search(r"N[uú]mero[:\s]*([1-4])", t, flags=re.I)
    if m:
        n_num = int(m.group(1))

    if (n_paren is not None) and (n_num is not None) and (n_paren != n_num):
        return None, None

    n_obs = n_paren if n_paren is not None else n_num
    if n_obs is None:
        return None, None

    return ("GREEN" if has_green and not has_red else "RED"), n_obs

# =========================
# Models
# =========================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None

# =========================
# Routes
# =========================
@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>"}

# GET/HEAD do webhook (evita 404)
@app.get("/webhook/{token}")
@app.head("/webhook/{token}")
async def webhook_get(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    host = request.headers.get("x-forwarded-host") or request.url.hostname or "localhost"
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    base = f"{scheme}://{host}".rstrip("/")
    return {
        "ok": True,
        "detail": "Webhook ativo. O Telegram deve usar POST.",
        "webhook_post_url": f"{base}/webhook/{WEBHOOK_TOKEN}",
        "telegram_setwebhook": (
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setWebhook?url={base}/webhook/{WEBHOOK_TOKEN}"
        )
    }

# POST do webhook (processa mensagens do Telegram)
@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message
    if not msg:
        return {"ok": True}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    event_kind, n_observed = _detect_outcome_clear(text)
    if event_kind and n_observed is not None:
        await tg_broadcast(f"{event_kind} detectado com número {n_observed}")
        return {"ok": True, "observed": n_observed, "kind": event_kind}

    return {"ok": True, "skipped": True}
