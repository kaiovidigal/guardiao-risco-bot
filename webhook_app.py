# -*- coding: utf-8 -*-
# Fantan — Número SECO em tempo real + aprendizado por COMBINAÇÃO (SQLite)
# - Sempre envia 1 número (sem pular sinal)
# - Aprende no fechamento (GREEN/RED), persistente entre deploys
# - Logs de decisão (DEBUG_SUGGEST=1)

import os, re, time, math, json, logging, sqlite3, asyncio
from typing import Any, Dict, List, Optional, Tuple

import nest_asyncio
nest_asyncio.apply()

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx
from aiogram import Bot

# =========================
# Config via ENV
# =========================
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", os.getenv("DESTINO_CHAT_ID","0")) or "0")
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()  # ex: @fantanvidigal
PUBLIC_URL     = (os.getenv("PUBLIC_URL", "") or os.getenv("RENDER_EXTERNAL_URL","")).rstrip("/")
BOT_ENABLED    = int(os.getenv("BOT_ENABLED", "1"))
DEBUG_SUGGEST  = int(os.getenv("DEBUG_SUGGEST", "1"))
Z_WILSON       = float(os.getenv("Z_WILSON", "1.96"))
DB_PATH        = os.getenv("DB_PATH", "data/retro.db")
COOLDOWN_S     = int(os.getenv("COOLDOWN_S", "0"))   # 0 = sem cooldown (não perder sinal)
DEDUP_TTL_S    = int(os.getenv("DEDUP_TTL_S", "0"))  # 0 = sem dedupe
REC_BONUS_H    = int(os.getenv("REC_BONUS_H", "6"))  # bônus de recência (horas)
REC_BONUS      = float(os.getenv("REC_BONUS", "0.02"))

# Mensagens que reconhecemos
RE_ENTRY  = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
RE_SEQ    = re.compile(r"Sequ[eê]ncia[:\s]*([^\n]+)", re.I)
RE_G1     = re.compile(r"\bG1\b", re.I)
RE_CLOSE  = re.compile(r"APOSTA\s+ENCERRADA", re.I)
RE_GREEN  = re.compile(r"\b(GREEN|VERDE)\b", re.I)
RE_RED    = re.compile(r"\bRED\b|\bLOSS\b", re.I)
RE_NUM    = re.compile(r"\b([1-4])\b")

# =========================
# App & Log
# =========================
app = FastAPI()
log = logging.getLogger("fantan-seco")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# =========================
# SQLite (persistente)
# =========================
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS combo_stats(
        combo TEXT PRIMARY KEY,               -- ex.: "G2|2-3"  ou "G1|4-3-2"
        win1  INTEGER NOT NULL DEFAULT 0,
        win2  INTEGER NOT NULL DEFAULT 0,
        win3  INTEGER NOT NULL DEFAULT 0,
        win4  INTEGER NOT NULL DEFAULT 0,
        last1 INTEGER NOT NULL DEFAULT 0,     -- último ts de acerto
        last2 INTEGER NOT NULL DEFAULT 0,
        last3 INTEGER NOT NULL DEFAULT 0,
        last4 INTEGER NOT NULL DEFAULT 0
    );
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS meta(
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
    );
    """)
    return conn

DB = _db()

def meta_set(k:str, v:Any):
    with DB:
        DB.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

def meta_get(k:str, default=None):
    cur = DB.execute("SELECT v FROM meta WHERE k=?", (k,))
    r = cur.fetchone()
    return json.loads(r[0]) if r else default

# =========================
# Estado em memória
# =========================
# dedupe por message_id (timestamp para expirar)
_seen_msgs: Dict[int, float] = {}  # message_id -> ts
_last_sent_ts: float = 0.0

# pareamento simples por chat: última combinação vista
_last_combo_by_chat: Dict[int, Dict[str, Any]] = {}  # chat_id -> {"modo","nums","ts"}

# desempate determinístico se empatar score
FIX_TIE_ORDER = [2,3,1,4]

# =========================
# Util
# =========================
def now() -> float: return time.time()

def wilson_lower(p:float, n:float, z:float=Z_WILSON)->float:
    if n <= 0: return 0.0
    denom  = 1 + (z*z)/n
    centre = p + (z*z)/(2*n)
    margin = z * math.sqrt((p*(1-p)/n) + (z*z)/(4*n*n))
    return max(0.0, (centre - margin)/denom)

def make_combo_key(modo:str, candidatos:List[int]) -> str:
    return f"{modo.upper()}|{'-'.join(map(str, candidatos))}"

def record_outcome(modo:str, candidatos:List[int], winner:int, ts:int|None=None):
    if ts is None: ts = int(now())
    key = make_combo_key(modo, candidatos)
    col_win  = f"win{winner}"
    col_last = f"last{winner}"
    with DB:
        DB.execute("INSERT OR IGNORE INTO combo_stats(combo) VALUES(?)", (key,))
        DB.execute(f"UPDATE combo_stats SET {col_win}={col_win}+1, {col_last}=? WHERE combo=?", (ts, key))
    if DEBUG_SUGGEST:
        log.info(f"[LEARN] combo={key} -> winner={winner}")

def get_combo_stats(modo:str, candidatos:List[int]):
    key = make_combo_key(modo, candidatos)
    cur = DB.execute("SELECT win1,win2,win3,win4,last1,last2,last3,last4 FROM combo_stats WHERE combo=?", (key,))
    row = cur.fetchone()
    if not row:
        return key, {1:0,2:0,3:0,4:0}, {1:0,2:0,3:0,4:0}
    wins = {1:row[0],2:row[1],3:row[2],4:row[3]}
    last = {1:row[4],2:row[5],3:row[6],4:row[7]}
    return key, wins, last

def parse_candidates(text:str) -> Tuple[Optional[List[int]], Optional[str]]:
    m = RE_SEQ.search(text or "")
    if not m:
        return None, None
    seq = m.group(1)
    nums = [int(x) for x in re.findall(r"[1-4]", seq)]
    if len(nums) not in (2,3):
        return None, None
    modo = "G1" if RE_G1.search(text) else "G2"
    return nums, modo

def parse_close(text:str) -> Optional[Dict[str,Any]]:
    if RE_CLOSE.search(text) or RE_GREEN.search(text) or RE_RED.search(text):
        win = None
        mw = RE_NUM.search(text)
        if mw:
            win = int(mw.group(1))
        return {"close": True, "winner": win, "green": bool(RE_GREEN.search(text))}
    return None

async def ensure_webhook():
    if not (TG_BOT_TOKEN and PUBLIC_URL):
        log.warning("Webhook NÃO registrado (faltando TG_BOT_TOKEN ou PUBLIC_URL).")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setWebhook"
    hook = f"{PUBLIC_URL}/webhook/{TG_BOT_TOKEN}"
    data = {"url": hook, "drop_pending_updates": True}
    try:
        async with httpx.AsyncClient(timeout=20) as s:
            r = await s.post(url, data=data)
            js = r.json()
        if js.get("ok"):
            log.info("🌐 Webhook registrado em %s", hook)
        else:
            log.warning("Falha setWebhook: %s", js)
    except Exception as e:
        log.error("setWebhook erro: %s", e)

async def send_text(chat_id:int, text:str):
    if not BOT_ENABLED:
        if DEBUG_SUGGEST: log.info("[SEND] pulado (BOT_ENABLED=0): %s", text)
        return
    try:
        bot = Bot(token=TG_BOT_TOKEN, parse_mode="HTML")
        await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
        await bot.session.close()
    except Exception as e:
        log.error("Falha ao enviar mensagem: %s", e)

def should_skip_by_dedupe(msg_id:int)->bool:
    if DEDUP_TTL_S <= 0:
        return False
    ts = now()
    # limpa antigos
    for k,v in list(_seen_msgs.items()):
        if ts - v > DEDUP_TTL_S:
            _seen_msgs.pop(k, None)
    if msg_id in _seen_msgs:
        return True
    _seen_msgs[msg_id] = ts
    return False

def cooldown_ok() -> bool:
    global _last_sent_ts
    if COOLDOWN_S <= 0:
        return True
    if now() - _last_sent_ts >= COOLDOWN_S:
        _last_sent_ts = now()
        return True
    return False

def choose_single(modo:str, candidatos:List[int]) -> Dict[str,Any]:
    """
    Escolhe SEMPRE 1 número com base na combinação:
      - p_hat com Laplace: (wins+1)/(sum(wins_cand)+len(cand))
      - Wilson lower bound como 'confiança'
      - bônus de recência se last < REC_BONUS_H
      - desempate determinístico por FIX_TIE_ORDER
    """
    key, wins, last = get_combo_stats(modo, candidatos)
    total = sum(wins[c] for c in candidatos)
    n_eff = max(1.0, float(total))  # simples
    rows = []
    now_ts = int(now())

    for n in candidatos:
        w = wins[n]
        p_hat = (w + 1.0) / (total + len(candidatos) + 1e-9)
        wl = wilson_lower(p_hat, n_eff, Z_WILSON)
        rec = REC_BONUS if (last[n] and (now_ts - last[n] < REC_BONUS_H*3600)) else 0.0
        score = wl + rec
        rows.append((score, wl, p_hat, rec, n))

    # ordena por score; se empatar, usa ordem fixa
    rows.sort(key=lambda t: (t[0], -FIX_TIE_ORDER.index(t[4]) if t[4] in FIX_TIE_ORDER else 99), reverse=True)
    choice = rows[0][4]
    return {
        "choice": choice,
        "key": key,
        "obs": int(total),
        "rows": [{"n":r[4],"score":round(r[0],4),"wl":round(r[1],4),"p":round(r[2],4),"rec":round(r[3],4)} for r in rows]
    }

async def handle_signal(chat_id:int, msg_id:int, text:str):
    # filtro por canal de origem, se PUBLIC_CHANNEL estiver definido (username do chat)
    # (Nota: updates de canal enviados ao webhook trazem channel_post com chat.username)
    # O filtro por chat.username é feito no webhook abaixo, aqui focamos apenas na lógica.

    # dedupe / cooldown
    if should_skip_by_dedupe(msg_id):
        if DEBUG_SUGGEST: log.info("[SKIP] dedupe msg_id=%s", msg_id)
        return
    if not cooldown_ok():
        if DEBUG_SUGGEST: log.info("[SKIP] cooldown")
        return

    cand, modo = parse_candidates(text)
    if not cand:
        if DEBUG_SUGGEST: log.info("[SKIP] sem candidatos (Sequência não detectada)")
        return
    choice = choose_single(modo, cand)
    if DEBUG_SUGGEST:
        log.info(f"[SUGGEST] {choice['key']} -> choice={choice['choice']} rows={choice['rows']} obs={choice['obs']}")
    msg = (
        f"🎯 <b>Número seco sugerido:</b> <code>{choice['choice']}</code>\n"
        f"📊 <i>Base:</i> ~{choice['obs']} (combinação {choice['key']})\n"
        f"✅ Conf. (Wilson): <b>{choice['rows'][0]['wl']*100:.2f}%</b>"
    )
    # envia para o destino configurado; se não houver, responde no próprio chat
    dest = TARGET_CHAT_ID if TARGET_CHAT_ID != 0 else chat_id
    await send_text(dest, msg)
    _last_combo_by_chat[chat_id] = {"modo": modo, "nums": cand, "ts": int(now())}

async def handle_close(chat_id:int, text:str):
    # tenta obter vencedor explícito
    m = RE_NUM.search(text or "")
    winner = int(m.group(1)) if m else None
    # pega a última combinação vista para esse chat
    last = _last_combo_by_chat.get(chat_id)
    if not last or winner not in (1,2,3,4):
        if DEBUG_SUGGEST: log.info("[CLOSE] sem last combo ou sem winner")
        return
    record_outcome(last["modo"], last["nums"], winner)

# =========================
# Webhook
# =========================
def _validate_token(token_in_path: str):
    if TG_BOT_TOKEN == "" or token_in_path != TG_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    _validate_token(token)
    data = await request.json()
    upd = data
    msg = upd.get("channel_post") or upd.get("message") or {}
    text = msg.get("text") or msg.get("caption") or ""
    chat = msg.get("chat") or {}
    chat_id = int(chat.get("id") or 0)
    msg_id  = int(msg.get("message_id") or 0)

    # Se PUBLIC_CHANNEL foi definido, filtre por username do canal de origem
    if PUBLIC_CHANNEL:
        username = (chat.get("username") or "").lower()
        if username != PUBLIC_CHANNEL.lstrip("@").lower():
            return JSONResponse({"ok":True, "skip":"origem diferente"})

    if not text:
        return JSONResponse({"ok": True})

    # Entrada confirmada → escolher e enviar SECO
    if RE_ENTRY.search(text):
        await handle_signal(chat_id, msg_id, text)
        return JSONResponse({"ok": True, "handled": "entry"})

    # Fechamento → aprender vencedor
    if RE_CLOSE.search(text) or RE_GREEN.search(text) or RE_RED.search(text):
        await handle_close(chat_id, text)
        return JSONResponse({"ok": True, "handled": "close"})

    # Nenhum caso relevante
    return JSONResponse({"ok": True})

# =========================
# Status & Saúde
# =========================
@app.get("/")
async def root():
    return {
        "ok": True,
        "bot_enabled": bool(BOT_ENABLED),
        "public_channel": PUBLIC_CHANNEL,
        "target_chat_id": TARGET_CHAT_ID,
        "cooldown_s": COOLDOWN_S,
        "dedup_ttl_s": DEDUP_TTL_S,
        "webhook": f"/webhook/{TG_BOT_TOKEN[:10]}..." if TG_BOT_TOKEN else ""
    }

@app.get("/status")
async def status():
    # retorna top combos e stats resumidos
    rows = []
    cur = DB.execute("SELECT combo, win1,win2,win3,win4 FROM combo_stats ORDER BY (win1+win2+win3+win4) DESC LIMIT 15")
    for combo, w1,w2,w3,w4 in cur.fetchall():
        tot = w1+w2+w3+w4
        if tot <= 0: continue
        rows.append({"combo": combo, "total": tot, "w": {1:w1,2:w2,3:w3,4:w4}})
    return {"ok": True, "combos": rows, "now": int(now())}

# =========================
# Startup
# =========================
@app.on_event("startup")
async def on_startup():
    if TG_BOT_TOKEN and PUBLIC_URL:
        await ensure_webhook()
    log.info("✅ Serviço ativo. BOT_ENABLED=%s | TARGET_CHAT_ID=%s | PUBLIC_CHANNEL=%s",
             BOT_ENABLED, TARGET_CHAT_ID, PUBLIC_CHANNEL)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("webhook_app:app", host="0.0.0.0", port=port)