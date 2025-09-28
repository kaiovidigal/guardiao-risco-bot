#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
webhook_app.py — G0 only — 30 especialistas (E1..E30)
- Sem gales: decisão única (G0) por consenso de 30 especialistas
- Configurações embutidas (sem ENV)
- Dedupe por update_id (tabela processed)
- Filtro por SOURCE_CHANNEL
- Encaminha SUGESTÃO para TARGET_CHANNEL
- Fecha pendências só com o primeiro observado (G0)
- DB SQLite (WAL + busy_timeout)

Rotas:
  GET  /                -> ok
  GET  /health          -> status
  POST /webhook/meusegredo123  -> endpoint do Telegram
"""

import os, re, time, sqlite3, math, json, random
from contextlib import contextmanager
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone

# ========= CONFIG FIXA =========
TG_BOT_TOKEN   = "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4"
WEBHOOK_TOKEN  = "meusegredo123"
TARGET_CHANNEL = "-1002796105884"
SOURCE_CHANNEL = "-1002810508717"
DB_PATH        = "/var/data/data.db"
TZ_NAME        = "America/Sao_Paulo"

DEBUG_MSG      = False     # enviar mensagens de debug no canal destino
BYPASS_SOURCE  = False     # se True, não filtra SOURCE_CHANNEL

# ========= Imports web =========
import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= Timezone =========
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(TZ_NAME)
except Exception:
    _TZ = timezone.utc

# ========= App =========
app = FastAPI(title="guardiao-g0-30x", version="1.0.0")

# ========= Utils =========
def now_ts() -> int:
    return int(time.time())

def ts_str(ts: Optional[int] = None) -> str:
    if ts is None: ts = now_ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _norm(d: Dict[int, float]) -> Dict[int, float]:
    s = float(sum(d.values())) or 1e-9
    return {k: max(0.0, v)/s for k, v in d.items()}

def _softmax(d: Dict[int, float], t: float = 1.0) -> Dict[int, float]:
    if t <= 0: t = 1e-6
    m = max(d.values()) if d else 0.0
    ex = {k: math.exp((v - m) / t) for k, v in d.items()}
    s = sum(ex.values()) or 1e-9
    return {k: v/s for k, v in ex.items()}

def _gap_top2(dist: Dict[int,float]) -> float:
    r = sorted(dist.values(), reverse=True)
    if not r: return 0.0
    if len(r) == 1: return r[0]
    return max(0.0, r[0]-r[1])

# ========= DB =========
def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=10000;")
    return con

@contextmanager
def _tx():
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def _ensure_tables():
    con = _connect(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS processed(
        update_id TEXT PRIMARY KEY,
        seen_at   INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram(
        n INTEGER NOT NULL, ctx TEXT NOT NULL, nxt INTEGER NOT NULL, w REAL NOT NULL,
        PRIMARY KEY (n, ctx, nxt)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS feedback(
        n INTEGER NOT NULL, ctx TEXT NOT NULL, nxt INTEGER NOT NULL, w REAL NOT NULL,
        PRIMARY KEY (n, ctx, nxt)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending(
        id INTEGER PRIMARY