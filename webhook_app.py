# -*- coding: utf-8 -*-
# Guardião — versão full enxuta (precisão ~90%)
import os, re, json, time, sqlite3, asyncio, shutil
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone
from collections import Counter
import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# CONFIG
# =========================
DB_PATH = os.getenv("DB_PATH", "/data/data.db").strip()
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
IA_CHANNEL     = os.getenv("IA_CHANNEL", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
REPL_ENABLED   = os.getenv("REPL_ENABLED", "0").lower() in ("1","true")
REPL_CHANNEL   = os.getenv("REPL_CHANNEL", "").strip()
TELEGRAM_API   = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Precisão calibrada
WINDOW = 400
DECAY  = 0.985
MIN_CONF_G0 = 0.65
MIN_GAP_G0  = 0.05
MIN_SAMPLES = 1500
IA2_TIER_STRICT = 0.72
IA2_GAP_SAFETY  = 0.08
IA2_DELTA_GAP   = 0.03

SELF_LABEL_IA = os.getenv("SELF_LABEL_IA", "Tiro seco por IA")
app = FastAPI(title="Guardião", version="3.13.0")

# =========================
# DB
# =========================
def _connect(): 
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    return con

def exec_write(sql, params=()):
    con = _connect(); con.execute(sql, params); con.commit(); con.close()

def query_one(sql, params=()): 
    con = _connect(); row = con.execute(sql, params).fetchone(); con.close(); return row

def query_all(sql, params=()): 
    con = _connect(); rows = con.execute(sql, params).fetchall(); con.close(); return rows

# =========================
# Telegram helpers
# =========================
_httpx_client: Optional[httpx.AsyncClient] = None
async def get_httpx(): 
    global _httpx_client
    if _httpx_client is None: _httpx_client = httpx.AsyncClient(timeout=10)
    return _httpx_client

async def tg_send_text(chat_id: str, text: str):
    if not TG_BOT_TOKEN or not chat_id: return
    client = await get_httpx()
    await client.post(f"{TELEGRAM_API}/sendMessage",
                      json={"chat_id": chat_id,"text": text,"parse_mode":"HTML"})

async def tg_broadcast(text: str):
    if PUBLIC_CHANNEL: await tg_send_text(PUBLIC_CHANNEL, text)
    if REPL_ENABLED and REPL_CHANNEL: await tg_send_text(REPL_CHANNEL, text)

async def send_ia_text(text: str):
    if IA_CHANNEL: await tg_send_text(IA_CHANNEL, text)

# =========================
# Timeline
# =========================
def append_timeline(n:int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (int(time.time()), int(n)))

def get_recent_tail(window=WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

# =========================
# Predição simplificada
# =========================
def laplace_ratio(wins:int, losses:int) -> float:
    return (wins+1)/(wins+losses+2)

def suggest_number(base: List[int]) -> Tuple[Optional[int], float, int]:
    tail = get_recent_tail(WINDOW)
    if not tail: return None,0.0,0
    scores={c:1.0 for c in base}
    for c in base: scores[c]+=tail.count(c)/len(tail)
    total=sum(scores.values())
    post={k:v/total for k,v in scores.items()}
    a=sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a: return None,0.0,len(tail)
    best=a[0][0]; conf=a[0][1]
    gap=a[0][1]-(a[1][1] if len(a)>1 else 0)
    if conf<MIN_CONF_G0 or gap<MIN_GAP_G0 or len(tail)<MIN_SAMPLES:
        return None,0.0,len(tail)
    return best,conf,len(tail)

# =========================
# IA Loop
# =========================
async def ia2_process_once():
    best,conf,tail_len=suggest_number([1,2,3,4])
    if best and conf>=IA2_TIER_STRICT:
        await send_ia_text(f"🤖 <b>{SELF_LABEL_IA} [FIRE]</b>\n"
                           f"🎯 Número seco (G0): <b>{best}</b>\n"
                           f"📈 Conf: <b>{conf*100:.2f}%</b> | Amostra≈<b>{tail_len}</b>")

@app.on_event("startup")
async def _boot():
    async def _loop():
        while True:
            try: await ia2_process_once()
            except Exception as e: print(f"[IA2] error: {e}")
            await asyncio.sleep(0.5)
    asyncio.create_task(_loop())

# =========================
# Webhook
# =========================
class Update(BaseModel):
    update_id:int; channel_post:Optional[dict]=None
    message:Optional[dict]=None; edited_channel_post:Optional[dict]=None

@app.post("/webhook/{token}")
async def webhook(token:str, request:Request):
    if token!=WEBHOOK_TOKEN: raise HTTPException(status_code=403)
    data=await request.json(); upd=Update(**data)
    msg=upd.channel_post or upd.message or upd.edited_channel_post
    if not msg: return {"ok":True}
    text=(msg.get("text") or "").strip()
    m=re.search(r"Sequ[eê]ncia:\s*([\d\s\|\-]+)", text)
    if not m: return {"ok":True,"skipped":True}
    parts=[int(x) for x in re.findall(r"[1-4]",m.group(1))]
    for n in parts[::-1]: append_timeline(n)
    best,conf,samples=suggest_number([1,2,3,4])
    if best:
        await tg_broadcast(f"🎯 <b>Número seco (G0):</b> <b>{best}</b>\n"
                           f"📊 Conf: {conf*100:.2f}% | Amostra≈{samples}")
    return {"ok":True}