# -*- coding: utf-8 -*-
# Fan Tan — Guardião (IA via trigrama + cauda, Fast-path réplica)
#
# - IA decide pelo contexto de 3 números (trigrama) confirmado pela cauda(40)
# - Recuperação G1/G2 oculta (converte LOSS -> GREEN)
# - Placar separado Canal/IA
# - Webhook com fast-path: espelha imediatamente no canal réplica
#
# Procfile exemplo:
# web: gunicorn webhook_app:app --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT

import os, re, json, time, sqlite3, asyncio
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone
from collections import Counter

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
DB_PATH = os.getenv("DB_PATH", "/data/data.db")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "-1003052132833")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
app = FastAPI()

# =========================
# DB helpers
# =========================
def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    con = _connect()
    row = con.execute(sql, params).fetchone()
    con.close()
    return row

def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    con = _connect()
    rows = con.execute(sql, params).fetchall()
    con.close()
    return rows

def exec_write(sql: str, params: tuple = ()):
    con = _connect()
    con.execute(sql, params)
    con.commit()
    con.close()

# =========================
# Telegram client (pool)
# =========================
_HTTPX_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)
_HTTPX_TIMEOUT = httpx.Timeout(5.0, 10.0, 10.0)
_tg_client: Optional[httpx.AsyncClient] = None

async def _ensure_tg_client():
    global _tg_client
    if _tg_client is None:
        _tg_client = httpx.AsyncClient(timeout=_HTTPX_TIMEOUT, limits=_HTTPX_LIMITS)

async def tg_send_text(chat_id: str, text: str):
    if not TG_BOT_TOKEN or not chat_id:
        return
    await _ensure_tg_client()
    try:
        await _tg_client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        )
    except Exception as e:
        print(f"[TG] erro: {e}")

# =========================
# Cauda + Trigrama
# =========================
def get_recent_tail(window:int=400) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def tail_top2_boost(tail: List[int], k:int=40) -> Dict[int,float]:
    boosts = {1:1.0,2:1.0,3:1.0,4:1.0}
    if not tail: return boosts
    c = Counter(tail[-k:])
    if not c: return boosts
    freq = c.most_common()
    if len(freq) >= 1: boosts[freq[0][0]] = 1.04
    if len(freq) >= 2: boosts[freq[1][0]] = 1.02
    return boosts

def _ng_prob_ctx(ctx: List[int], candidate:int) -> float:
    n = len(ctx)+1
    if n < 2: return 0.0
    ck = ",".join(str(x) for x in ctx)
    row_tot = query_one("SELECT SUM(weight) as w FROM ngram_stats WHERE n=? AND ctx=?", (n,ck))
    tot = (row_tot["w"] or 0.0) if row_tot else 0.0
    if tot <= 0: return 0.0
    row_hit = query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n,ck,candidate))
    w = (row_hit["weight"] or 0.0) if row_hit else 0.0
    return w/tot

def posterior_trigram(tail: List[int]) -> Dict[int,float]:
    base = [1,2,3,4]
    scores = {k:1.0 for k in base}
    ctx3 = tail[-3:] if len(tail)>=3 else []
    for c in base:
        p = _ng_prob_ctx(ctx3,c) if ctx3 else 0.0
        scores[c] *= p if p>0 else 1e-3
    boosts = tail_top2_boost(tail,40)
    for c in base: scores[c] *= boosts.get(c,1.0)
    tot = sum(scores.values()) or 1e-9
    return {k:v/tot for k,v in scores.items()}

def choose_number(post: Dict[int,float]) -> Tuple[Optional[int],float,float]:
    if not post: return None,0.0,0.0
    top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    n1,p1 = top[0]
    p2 = top[1][1] if len(top)>1 else 0.0
    gap = p1-p2
    if gap < 0.015: return None,0.0,gap
    return n1,p1,gap

# =========================
# IA loop
# =========================
async def ia2_process_once():
    tail = get_recent_tail()
    post = posterior_trigram(tail)
    best,conf,gap = choose_number(post)
    if best is None: return
    await tg_send_text(PUBLIC_CHANNEL, f"🤖 IA FIRE
🎯 Número: {best}
Conf: {conf*100:.2f}% | Gap: {gap:.3f}")

# =========================
# Fast-path webhook
# =========================
class Update(BaseModel):
    update_id:int
    message:Optional[dict]=None
    channel_post:Optional[dict]=None

@app.post("/webhook/{token}")
async def webhook(token:str, request:Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message
    if not msg: return {"ok":True}
    text = (msg.get("text") or "").strip()
    # Fast espelho
    asyncio.create_task(tg_send_text(PUBLIC_CHANNEL, f"⚡️ Canal: {text[:50]}"))
    # Heavy (processo IA)
    asyncio.create_task(ia2_process_once())
    return {"ok":True,"fast_ack":True}

@app.get("/")
async def root():
    return {"ok":True,"detail":"Guardião ativo"}
