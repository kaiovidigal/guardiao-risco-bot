#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Webhook completo (com GEN_AUTO + validação estrita + blindagem contra 500/502)
"""
import os, re, time, sqlite3, math, json
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Dict, Tuple
import httpx
from fastapi import FastAPI, Request, HTTPException

# Config ENV
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "-1002796105884").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()
DB_PATH        = os.getenv("DB_PATH", "/var/data/data.db").strip()
DEBUG_MSG      = os.getenv("DEBUG_MSG", "0").strip() in ("1","true","True","yes")
BYPASS_SOURCE  = os.getenv("BYPASS_SOURCE", "0").strip() in ("1","true","True","yes")
GEN_AUTO       = os.getenv("GEN_AUTO", "1").strip() in ("1","true","True","yes")
TELEGRAM_API   = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI(title="Guardiao Bot", version="1.0")

# ========= Utils =========
def now_ts(): return int(time.time())

async def tg_send_text(chat_id: str, text: str):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    except Exception: pass

# ========= Regex =========
ENTRY_RX = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
ANALISANDO_RX = re.compile(r"\bANALISANDO\b", re.I)
GREEN_RX = re.compile(r"(?:\bgr+e+e?n\b|✅)", re.I)
LOSS_RX  = re.compile(r"(?:\blo+s+s?\b|❌)", re.I)
GALE1_RX = re.compile(r"1.?gale", re.I)
GALE2_RX = re.compile(r"2.?gale", re.I)

# ========= Funções de parsing dummy (ilustração) =========
def parse_entry_text(t: str): return {"seq":[1,2,3], "after":4} if ENTRY_RX.search(t) else None
def parse_analise_seq(t: str): return [1,2,3] if ANALISANDO_RX.search(t) else []
def parse_close_numbers(t: str): return [1,2,3]

# ========= Mock DB =========
_pending = None
def get_open_pending(): return _pending
def _open_pending_with_ctx(best, after, *ctx): global _pending; _pending={"suggested":best,"seen":""}; return True
def _seen_append(pend, seq): pend["seen"] += "-"+"-".join(seq)
def _seen_list(pend): return pend.get("seen","").split("-") if pend else []

# ========= Webhook =========
@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    # Aceita apenas o WEBHOOK_TOKEN
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        # Blindagem contra corpo inválido
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" not in ctype:
            return {"ok": True, "skipped": "non_json"}
        raw = await request.body()
        if len(raw) > 1_500_000:
            return {"ok": True, "skipped": "body_too_large"}
        try:
            data = json.loads(raw.decode("utf-8", errors="ignore"))
        except Exception:
            return {"ok": True, "skipped": "json_parse_error"}

        msg = data.get("channel_post") or data.get("message") or {}
        text = (msg.get("text") or "").strip()

        # Regras
        if ANALISANDO_RX.search(text):
            seq = parse_analise_seq(text)
            if GEN_AUTO and not get_open_pending():
                await tg_send_text(TARGET_CHANNEL, f"🤖 GEN_AUTO abriu após {seq[-1]}")
            return {"ok":True,"analise":seq}

        if ENTRY_RX.search(text):
            parsed = parse_entry_text(text)
            after = parsed["after"]
            best = 2
            _open_pending_with_ctx(best, after, None,None,None,None)
            await tg_send_text(TARGET_CHANNEL, f"🤖 <b>IA SUGERE</b> — {best} após {after}")
            return {"ok":True,"entrada":best}

        if GREEN_RX.search(text) or LOSS_RX.search(text):
            pend = get_open_pending()
            if pend:
                nums = parse_close_numbers(text)
                _seen_append(pend, [str(n) for n in nums])
                await tg_send_text(TARGET_CHANNEL, f"🔒 Fechando com observados: {pend['seen']}")
            return {"ok":True,"close":True}

        return {"ok":True,"skipped":"no_match"}
    except Exception as e:
        if DEBUG_MSG: await tg_send_text(TARGET_CHANNEL, f"DEBUG: erro {e!r}")
        return {"ok": True, "skipped": "internal_error"}

@app.get("/health")
async def health(): return {"ok": True}
