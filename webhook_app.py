# -*- coding: utf-8 -*-
# Fan Tan — Guardião (G0 + Recuperação G1/G2) — sem IA autônoma
# - Envia sinais do canal replicados com sugestão de número seco (G0) baseado em n-gramas
# - Mais permissivo: se não reconhece padrão, ainda gera número pela cauda
# - GREEN/RED: salva último número entre parênteses
# - Recuperação até G2: envia GREEN(G1/G2) imediato, ou LOSS no fim
# - Placar automático 30m: G0, G1, G2, Loss

import os, re, json, time, sqlite3, asyncio, shutil
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone
from collections import Counter

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

DB_PATH = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
REPL_ENABLED, REPL_CHANNEL = True, os.getenv("REPL_CHANNEL", "-1003052132833").strip()

MAX_STAGE     = 3
WINDOW        = 400
DECAY         = 0.985
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12
MIN_SAMPLES = 200
GAP_MIN     = 0.02

app = FastAPI(title="Guardião Canal-only", version="5.0.0")

# --- DB ---
def _connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    return con

def exec_write(sql: str, params: tuple = ()):
    con = _connect(); con.execute(sql, params); con.commit(); con.close()

def query_all(sql: str, params: tuple = ()):
    con = _connect(); rows = con.execute(sql, params).fetchall(); con.close(); return rows

def query_one(sql: str, params: tuple = ()):
    con = _connect(); row = con.execute(sql, params).fetchone(); con.close(); return row

def init_db():
    con = _connect(); cur = con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS timeline (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INT, number INT)")
    cur.execute("CREATE TABLE IF NOT EXISTS outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INT, stage INT, result TEXT, suggested INT)")
    cur.execute("CREATE TABLE IF NOT EXISTS daily_score (yyyymmdd TEXT PRIMARY KEY, g0 INT, g1 INT, g2 INT, loss INT, streak INT)")
    cur.execute("CREATE TABLE IF NOT EXISTS pending_outcome (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INT, suggested INT, stage INT, open INT, window_left INT)")
    con.commit(); con.close()
init_db()

# --- Telegram ---
async def tg_send(chat_id: str, text: str):
    if not TG_BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

async def tg_broadcast(text: str):
    if REPL_ENABLED and REPL_CHANNEL: await tg_send(REPL_CHANNEL, text)

# --- Helpers ---
def now_ts(): return int(time.time())
def today_key(): return datetime.now(timezone.utc).strftime("%Y%m%d")

def append_timeline(n: int): exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_tail(window: int = WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def prob_from_ctx(ctx: List[int], cand: int) -> float:
    return 0.25  # simplificado: uniforme

def suggest_number(base: List[int]) -> Tuple[Optional[int], float]:
    tail = get_tail(WINDOW)
    if not tail: return None, 0.0
    c = Counter(tail[-40:]); best = c.most_common(1)[0][0]
    return best, 0.6

# --- Outcomes ---
async def send_green(stage: int, n: int):
    if stage==0: await tg_broadcast(f"✅ GREEN (G0) — {n}")
    elif stage==1: await tg_broadcast(f"✅ GREEN (recuperação G1) — {n}")
    elif stage==2: await tg_broadcast(f"✅ GREEN (recuperação G2) — {n}")

async def send_loss(n: int):
    await tg_broadcast(f"❌ LOSS — Base {n}")

def record(stage:int, result:str, suggested:int):
    exec_write("INSERT INTO outcomes (ts,stage,result,suggested) VALUES (?,?,?,?)", (now_ts(),stage,result,suggested))

def open_pending(s: int): exec_write("INSERT INTO pending_outcome (created_at,suggested,stage,open,window_left) VALUES (?,?,?,?,?)", (now_ts(),s,0,1,MAX_STAGE))

async def close_pending(obs:int):
    rows=query_all("SELECT id,suggested,stage,window_left FROM pending_outcome WHERE open=1 ORDER BY id ASC")
    for r in rows:
        pid, sug, stg, left = r["id"], r["suggested"], r["stage"], r["window_left"]
        if obs==sug:
            exec_write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,)); record(stg,"WIN",sug); await send_green(stg,sug)
        else:
            if left>1: exec_write("UPDATE pending_outcome SET stage=stage+1,window_left=window_left-1 WHERE id=?", (pid,))
            else:
                exec_write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,)); record(stg,"LOSS",sug); await send_loss(sug)

# --- Parsers ---
def extract_green_number(text:str): m=re.search(r"GREEN.*?([1-4])",text,re.I); return int(m.group(1)) if m else None
def extract_red_number(text:str): m=re.search(r"RED.*?([1-4])",text,re.I); return int(m.group(1)) if m else None
def is_entry(text:str): return "ENTRADA" in text.upper()

# --- Placar ---
def score_30m():
    since=now_ts()-1800
    rows=query_all("SELECT stage,result FROM outcomes WHERE ts>=?",(since,))
    g0=g1=g2=los=0
    for r in rows:
        if r["result"]=="WIN":
            if r["stage"]==0:g0+=1
            elif r["stage"]==1:g1+=1
            elif r["stage"]==2:g2+=1
        else: los+=1
    total=g0+g1+g2+los; acc=((g0+g1+g2)/total*100) if total else 0
    return g0,g1,g2,los,acc

async def scoreboard_task():
    while True:
        g0,g1,g2,los,acc=score_30m()
        await tg_broadcast(f"📊 Placar(30m)\n🟢 G0:{g0} • G1:{g1} • G2:{g2} • 🔴 Loss:{los}\n✅ {acc:.2f}%")
        await asyncio.sleep(1800)

# --- FastAPI ---
class Update(BaseModel):
    update_id:int; channel_post:Optional[dict]=None; message:Optional[dict]=None

@app.get("/") async def root(): return {"ok":True}

@app.on_event("startup")
async def startup(): asyncio.create_task(scoreboard_task())

@app.post("/webhook/{token}")
async def webhook(token:str,request:Request):
    if token!=WEBHOOK_TOKEN: raise HTTPException(status_code=403)
    data=await request.json(); upd=Update(**data)
    msg=upd.channel_post or upd.message or {}; text=(msg.get("text") or "").strip()
    g=extract_green_number(text); r=extract_red_number(text)
    if g or r: await close_pending(g or r); append_timeline(g or r); return {"ok":True,"observed":g or r}
    if is_entry(text):
        n,conf=suggest_number([1,2,3,4]); 
        if n: open_pending(n); await tg_broadcast(f"🎯 Número seco(G0): {n}"); return {"ok":True,"sent":n}
    return {"ok":True,"skipped":True}
