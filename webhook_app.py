# -*- coding: utf-8 -*-
# webhook_app.py — versão enxuta ajustada
# - Sinal FIRE mais rápido (delay reduzido)
# - Removidas mensagens de IA FIRE, relatórios de desempenho e métricas extras
# - Mantida recuperação G1/G2 e extratora original
# - Calibragem de confiança e amostras melhorada

import os, re, json, time, sqlite3, asyncio
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone
from collections import Counter

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# ENV / CONFIG
# =========================
DB_PATH = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
REPL_CHANNEL   = os.getenv("REPL_CHANNEL", "").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Ajustes de thresholds e velocidade
WINDOW = 400
DECAY  = 0.985
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12
ALPHA, BETA, GAMMA = 1.05, 0.70, 0.40
MIN_SAMPLES = 500   # reduzi para soltar mais sinais
MIN_CONF_G0 = 0.52
MIN_GAP_G0  = 0.03

INTEL_ANALYZE_INTERVAL = 0.5  # mais rápido

app = FastAPI(title="Fantan Guardião — FIRE-only", version="3.12.0")

# =========================
# DB helpers
# =========================
def _ensure_db_dir():
    try: os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except: pass

def _connect() -> sqlite3.Connection:
    _ensure_db_dir()
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def exec_write(sql: str, params: tuple = ()):
    con = _connect(); con.execute(sql, params); con.commit(); con.close()

def query_all(sql: str, params: tuple = ()) -> list:
    con = _connect(); rows = con.execute(sql, params).fetchall(); con.close(); return rows

def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    con = _connect(); row = con.execute(sql, params).fetchone(); con.close(); return row

def init_db():
    con = _connect(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, number INTEGER NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram_stats (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, next INTEGER NOT NULL, weight REAL NOT NULL,
        PRIMARY KEY (n, ctx, next))""")
    con.commit(); con.close()

init_db()

# =========================
# Telegram helpers
# =========================
async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True})

async def tg_broadcast(text: str, parse: str="HTML"): 
    if REPL_CHANNEL: await tg_send_text(REPL_CHANNEL, text, parse)

# =========================
# Timeline & n-grams
# =========================
def append_timeline(n: int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (int(time.time()), int(n)))

def get_recent_tail(window: int = WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def prob_from_ngrams(ctx: List[int], candidate: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    row = query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key))
    tot = (row["w"] or 0.0) if row else 0.0
    if tot <= 0: return 0.0
    row2 = query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, candidate))
    w = (row2["weight"] or 0.0) if row2 else 0.0
    return w / tot

def ngram_backoff_score(tail: List[int], candidate: int) -> float:
    if not tail: return 0.0
    ctx4=tail[-4:] if len(tail)>=4 else []
    ctx3=tail[-3:] if len(tail)>=3 else []
    ctx2=tail[-2:] if len(tail)>=2 else []
    ctx1=tail[-1:] if len(tail)>=1 else []
    parts=[]
    if len(ctx4)==4: parts.append((W4, prob_from_ngrams(ctx4[:-1], candidate)))
    if len(ctx3)==3: parts.append((W3, prob_from_ngrams(ctx3[:-1], candidate)))
    if len(ctx2)==2: parts.append((W2, prob_from_ngrams(ctx2[:-1], candidate)))
    if len(ctx1)==1: parts.append((W1, prob_from_ngrams(ctx1[:-1], candidate)))
    return sum(w*p for w,p in parts)

def suggest_number() -> Tuple[Optional[int], float, int]:
    base=[1,2,3,4]; tail=get_recent_tail(WINDOW)
    scores={c:ngram_backoff_score(tail,c) for c in base}
    total=sum(scores.values()) or 1e-9
    post={k:v/total for k,v in scores.items()}
    a=sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a: return None,0.0,len(tail)
    gap=a[0][1]-(a[1][1] if len(a)>1 else 0.0)
    number=a[0][0] if (gap>=MIN_GAP_G0) else None
    conf=post.get(number,0.0) if number else 0.0
    row=query_one("SELECT SUM(weight) AS s FROM ngram_stats")
    samples=int((row["s"] or 0) if row else 0)
    if samples<MIN_SAMPLES or conf<MIN_CONF_G0: return None,0.0,samples
    return number, conf, samples

# =========================
# FastAPI
# =========================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None

@app.get("/")
async def root(): return {"ok":True,"samples":query_one("SELECT SUM(weight) AS s FROM ngram_stats")["s"]}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN: raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json(); msg=data.get("channel_post") or data.get("message") or {}
    text=(msg.get("text") or msg.get("caption") or "").strip()
    if not text: return {"ok":True}
    if re.search(r"ENTRADA\s+CONFIRMADA", text, flags=re.I):
        num,conf,samples=suggest_number()
        if num:
            await tg_broadcast(f"🎯 Número seco (G0): {num}\n📊 Conf: {conf*100:.2f}% | Amostra≈{samples}")
            return {"ok":True,"fire":num}
    return {"ok":True,"ignored":True}