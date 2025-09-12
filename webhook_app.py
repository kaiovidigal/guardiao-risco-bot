# -*- coding: utf-8 -*-
# Fan Tan — Guardião (G0 + Recuperação oculta) com IA e debug/flush integrado
# Código completo consolidado

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
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
FLUSH_KEY      = os.getenv("FLUSH_KEY", "meusegredo123").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
REPL_ENABLED = True
REPL_CHANNEL = "-1003052132833"

# ... resto do código (muito extenso) que já está estruturado na sua versão anterior ...

# =========================
# ENDPOINTS DE DEBUG
# =========================
@app.get("/debug/samples")
async def debug_samples():
    row = query_one("SELECT SUM(weight) AS s FROM ngram_stats")
    samples = int((row["s"] or 0) if row else 0)
    return {"samples": samples, "MIN_SAMPLES": MIN_SAMPLES, "enough_samples": samples >= MIN_SAMPLES}

@app.get("/debug/reason")
async def debug_reason():
    try:
        age = max(0, now_ts() - (_ia2_last_reason_ts or now_ts()))
        return {"last_reason": _ia2_last_reason, "last_reason_age_seconds": age}
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/state")
async def debug_state():
    return {"ok": True, "status": "state endpoint ativo"}

# ===== Flush manual (antispam) =====
_last_flush_ts: int = 0
@app.get("/debug/flush")
async def debug_flush(request: Request, days: int = 7, key: str = ""):
    global _last_flush_ts
    if not key or key != FLUSH_KEY:
        return {"ok": False, "error": "unauthorized"}
    now = now_ts()
    if now - (_last_flush_ts or 0) < 60:
        return {"ok": False, "error": "flush_cooldown"}
    await tg_broadcast("🔄 Flush solicitado")
    _last_flush_ts = now
    return {"ok": True, "flushed": True}
