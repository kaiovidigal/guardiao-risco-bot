#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webhook_fracionado.py — v1.0.0 (simples)
- Sem fluxo/pendências/gales/IA
- Apenas repassa mensagens do canal-fonte (por regex) para o TARGET_CHANNEL
- Limita a 20 sinais por janela (madrugada/dia/tarde/noite), reset diário no fuso
- Destino blindado no TARGET_CHANNEL
- Dedupe por update_id

ENV obrigatórias: TG_BOT_TOKEN, WEBHOOK_TOKEN
ENV opcionais:    TARGET_CHANNEL, SOURCE_CHANNEL, BYPASS_SOURCE, DEBUG_MSG
                  TZ_NAME, DB_PATH, SAFE_TARGET_MODE
                  QUOTA_PER_WINDOW, WIN_MADRUGADA, WIN_DIA, WIN_TARDE, WIN_NOITE
                  FILTER_REGEX
Webhook:          POST /webhook/{WEBHOOK_TOKEN}
"""
import os, re, time, sqlite3, json
from typing import Optional, Tuple
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= ENV =========
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "-1002796105884").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()   # se vazio, não filtra
BYPASS_SOURCE  = os.getenv("BYPASS_SOURCE", "0").strip() in ("1","true","True","yes","YES")
DEBUG_MSG      = os.getenv("DEBUG_MSG", "0").strip() in ("1","true","True","yes","YES")
SAFE_TARGET_MODE = os.getenv("SAFE_TARGET_MODE", "1").strip() in ("1","true","True","yes","YES")

TELEGRAM_API   = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Fuso e DB
TZ_NAME = os.getenv("TZ_NAME", "America/Sao_Paulo").strip()
_TZ = ZoneInfo(TZ_NAME) if ZoneInfo else timezone.utc
DB_PATH = os.getenv("DB_PATH", "/var/data/fracionado.db").strip() or "/var/data/fracionado.db"

# Quotas (20 por janela)
QUOTA_PER_WINDOW = int(os.getenv("QUOTA_PER_WINDOW", "20"))
WIN_MADRUGADA = os.getenv("WIN_MADRUGADA", "00:00-05:59")
WIN_DIA       = os.getenv("WIN_DIA",       "06:00-11:59")
WIN_TARDE     = os.getenv("WIN_TARDE",     "12:00-17:59")
WIN_NOITE     = os.getenv("WIN_NOITE",     "18:00-23:59")

# Filtro de mensagem (o que será considerado "sinal" para repassar)
FILTER_REGEX = os.getenv("FILTER_REGEX", r"ENTRADA\s+CONFIRMADA").strip()
FILTER_RX = re.compile(FILTER_REGEX, re.I)

def now_ts() -> int:
    return int(time.time())

def tz_today_ymd() -> str:
    dt = datetime.now(_TZ)
    return dt.strftime("%Y-%m-%d")

def _parse_range(txt: str) -> Tuple[Tuple[int,int], Tuple[int,int]]:
    a,b = txt.split("-")
    ah,am = [int(x) for x in a.split(":")]
    bh,bm = [int(x) for x in b.split(":")]
    return (ah,am),(bh,bm)

_RANGES = {
    "MADRUGADA": _parse_range(WIN_MADRUGADA),
    "DIA":       _parse_range(WIN_DIA),
    "TARDE":     _parse_range(WIN_TARDE),
    "NOITE":     _parse_range(WIN_NOITE),
}

def current_window() -> str:
    dt = datetime.now(_TZ)
    h, m = dt.hour, dt.minute
    t = h*60 + m
    for name, ((h1,m1),(h2,m2)) in _RANGES.items():
        t1 = h1*60 + m1
        t2 = h2*60 + m2
        if t1 <= t <= t2:
            return name
    return "DIA"

# ========= DB =========
def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def migrate_db():
    con = _connect(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS processed (
        update_id TEXT PRIMARY KEY,
        seen_at   INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS quota (
        ymd   TEXT NOT NULL,
        win   TEXT NOT NULL,
        used  INTEGER NOT NULL,
        PRIMARY KEY (ymd, win)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS state (
        id INTEGER PRIMARY KEY CHECK (id=1),
        last_reset_ymd TEXT DEFAULT ''
    )""")
    row = con.execute("SELECT 1 FROM state WHERE id=1").fetchone()
    if not row:
        cur.execute("INSERT INTO state (id, last_reset_ymd) VALUES (1, '')")
    con.commit(); con.close()
migrate_db()

def _is_processed(update_id: str) -> bool:
    if not update_id: return False
    con = _connect()
    row = con.execute("SELECT 1 FROM processed WHERE update_id=?", (update_id,)).fetchone()
    con.close()
    return bool(row)

def _mark_processed(update_id: str):
    if not update_id: return
    con = _connect(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO processed (update_id, seen_at) VALUES (?,?)", (str(update_id), now_ts()))
    con.commit(); con.close()

def _quota_used(ymd: str, win: str) -> int:
    con = _connect()
    row = con.execute("SELECT used FROM quota WHERE ymd=? AND win=?", (ymd, win)).fetchone()
    con.close()
    return int((row["used"] if row else 0) or 0)

def _quota_inc(ymd: str, win: str, inc: int=1):
    con = _connect(); cur = con.cursor()
    cur.execute("INSERT INTO quota(ymd,win,used) VALUES(?,?,?) ON CONFLICT(ymd,win) DO UPDATE SET used=used+?",
                (ymd, win, inc, inc))
    con.commit(); con.close()

def _get_last_reset_ymd() -> str:
    con = _connect()
    row = con.execute("SELECT last_reset_ymd FROM state WHERE id=1").fetchone()
    con.close()
    return (row["last_reset_ymd"] or "") if row else ""

def _set_last_reset_ymd(ymd: str):
    con = _connect(); cur = con.cursor()
    cur.execute("UPDATE state SET last_reset_ymd=? WHERE id=1", (ymd,))
    con.commit(); con.close()

def _daily_reset():
    today = tz_today_ymd()
    last = _get_last_reset_ymd()
    if last != today:
        # nada a apagar; o novo (ymd,win) começa do zero por chave primária
        _set_last_reset_ymd(today)

# ========= Telegram =========
async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN: return
    if SAFE_TARGET_MODE:
        chat_id = TARGET_CHANNEL
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{TELEGRAM_API}/sendMessage",
                              json={"chat_id": chat_id, "text": text, "parse_mode": parse,
                                    "disable_web_page_preview": True})
    except Exception:
        pass

# ========= App =========
if not TG_BOT_TOKEN: raise RuntimeError("Defina TG_BOT_TOKEN")
if not WEBHOOK_TOKEN: raise RuntimeError("Defina WEBHOOK_TOKEN")

app = FastAPI(title="webhook-fracionado", version="1.0.0")

@app.get("/")
async def root():
    _daily_reset()
    return {"ok": True, "service": "webhook-fracionado"}

@app.get("/health")
async def health():
    _daily_reset()
    ymd = tz_today_ymd()
    return {
        "ok": True,
        "time": datetime.now(timezone.utc).isoformat(),
        "tz": TZ_NAME,
        "quota": {
            "MADRUGADA": _quota_used(ymd, "MADRUGADA"),
            "DIA": _quota_used(ymd, "DIA"),
            "TARDE": _quota_used(ymd, "TARDE"),
            "NOITE": _quota_used(ymd, "NOITE"),
            "limit": QUOTA_PER_WINDOW
        }
    }

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    _daily_reset()

    data = await request.json()
    upd_id = str(data.get("update_id", "")) if isinstance(data, dict) else ""
    if _is_processed(upd_id):
        return {"ok": True, "skipped": "duplicate_update"}
    _mark_processed(upd_id)

    msg = data.get("channel_post") or data.get("message") \
        or data.get("edited_channel_post") or data.get("edited_message") or {}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    # filtro de origem (opcional)
    if SOURCE_CHANNEL and not BYPASS_SOURCE and chat_id != str(SOURCE_CHANNEL):
        if DEBUG_MSG:
            await tg_send_text(TARGET_CHANNEL, f"DEBUG: Ignorando chat {chat_id}, fonte esperada {SOURCE_CHANNEL}")
        return {"ok": True, "skipped": "wrong_source"}

    if not text:
        return {"ok": True, "skipped": "no_text"}

    # Só repassa se combinar com o filtro
    if not FILTER_RX.search(text):
        return {"ok": True, "skipped": "no_match_filter"}

    # Checa cota da janela
    ymd = tz_today_ymd()
    win = current_window()
    used = _quota_used(ymd, win)
    if used >= QUOTA_PER_WINDOW:
        if DEBUG_MSG:
            await tg_send_text(TARGET_CHANNEL, f"⚠️ Limite de {QUOTA_PER_WINDOW} sinais atingido para {win} ({ymd}).")
        return {"ok": True, "skipped": "quota_reached", "window": win, "used": used}

    # Envia para o canal alvo
    await tg_send_text(TARGET_CHANNEL, text, parse="HTML")
    _quota_inc(ymd, win, 1)

    return {"ok": True, "forwarded": True, "window": win, "used_now": used+1}
