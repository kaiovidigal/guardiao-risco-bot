# -*- coding: utf-8 -*-
# Guardião — Tiro Seco (versão enxuta v3.1 - fechamento rígido + sequência no aviso)
# Adições vs v3:
# - GREEN/LOSS exibem o número de conferência ao lado em parênteses: GREEN (1) / LOSS (4)
# - Mensagens incluem a linha "🚥 Sequência: a | b | c" com os números lidos na mensagem de fechamento
#
# Rotas:
#   POST /webhook/<WEBHOOK_TOKEN>
#   GET  /
#   GET  /debug/pending
#
# ENV obrigatórias: TG_BOT_TOKEN, WEBHOOK_TOKEN
# ENV opcionais: DB_PATH (default: /data/data.db), REPL_CHANNEL (-100... ou @canal)

import os, re, time, sqlite3
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# CONFIG / ENV
# =========================
DB_PATH       = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
TG_BOT_TOKEN  = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
REPL_CHANNEL  = os.getenv("REPL_CHANNEL", "").strip() or "-1003052132833"  # ajuste se necessário

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

MAX_STAGE = 3   # G0, G1, G2
WINDOW    = 400
MIN_SAMPLES = 120   # relaxado para não travar em cenários com pouco histórico
MIN_CONF   = 0.48   # relaxado

app = FastAPI(title="Guardião — Tiro Seco (lite v3.1)", version="1.3.1")

# =========================
# SQLite helpers
# =========================
def _ensure_db_dir():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except Exception:
        pass

def _connect() -> sqlite3.Connection:
    _ensure_db_dir()
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def exec_write(sql: str, params: tuple = ()):
    con = _connect(); con.execute(sql, params); con.commit(); con.close()

def query_all(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    con = _connect(); rows = con.execute(sql, params).fetchall(); con.close(); return rows

def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    con = _connect(); row = con.execute(sql, params).fetchone(); con.close(); return row

def init_db():
    con = _connect()
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram_stats (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, next INTEGER NOT NULL, weight REAL NOT NULL,
        PRIMARY KEY (n, ctx, next)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_outcome (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        suggested INTEGER NOT NULL,
        stage INTEGER NOT NULL,
        open INTEGER NOT NULL,
        window_left INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS suggestions (
        source_msg_id INTEGER PRIMARY KEY,
        suggested_number INTEGER NOT NULL,
        sent_at INTEGER NOT NULL
    )""")
    con.commit(); con.close()

init_db()

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
    if REPL_CHANNEL:
        await tg_send_text(REPL_CHANNEL, text, parse)

def _seq_str(numbers: List[int]) -> str:
    return " | ".join(str(n) for n in numbers) if numbers else "—"

# Mensagens com número de conferência + sequência
async def send_green(sugerido:int, stage_txt:str, conferido:int, seq:List[int]):
    await tg_broadcast(
        f"✅ <b>GREEN</b> (<b>{conferido}</b>) em <b>{stage_txt}</b> — Número: <b>{sugerido}</b> • Conf: <b>{conferido}</b>\n"
        f"🚥 <b>Sequência:</b> {_seq_str(seq)}"
    )

async def send_loss(sugerido:int, stage_txt:str, conferido:int, seq:List[int]):
    await tg_broadcast(
        f"❌ <b>LOSS</b> (<b>{conferido}</b>) — Número: <b>{sugerido}</b> ({stage_txt}) • Conf: <b>{conferido}</b>\n"
        f"🚥 <b>Sequência:</b> {_seq_str(seq)}"
    )

async def send_sinal_g0(num:int, conf:float, samples:int):
    await tg_broadcast(
        f"🤖 <b>Tiro seco</b>\n"
        f"🎯 Número seco (G0): <b>{num}</b>\n"
        f"📈 Conf: <b>{conf*100:.1f}%</b> | Amostra≈<b>{samples}</b>"
    )

# =========================
# Timeline & n-gram (mínimo)
# =========================
def append_timeline(n: int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_tail(window:int=WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def update_ngrams(window:int=WINDOW, decay:float=0.985, max_n:int=3):
    tail = get_tail(window)
    if len(tail) < 2: return
    for t in range(1, len(tail)):
        nxt = tail[t]
        dist = (len(tail)-1) - t
        w = (decay ** dist)
        for n in range(2, max_n+1):
            if t-(n-1) < 0: break
            ctx = tail[t-(n-1):t]
            ctx_key = ",".join(str(x) for x in ctx)
            exec_write("""
                INSERT INTO ngram_stats (n, ctx, next, weight)
                VALUES (?,?,?,?)
                ON CONFLICT(n, ctx, next) DO UPDATE SET weight = weight + excluded.weight
            """, (n, ctx_key, int(nxt), float(w)))

def prob_from_ngrams(ctx: List[int], candidate: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 3: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    row = query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key))
    tot = (row["w"] or 0.0) if row else 0.0
    if tot <= 0: return 0.0
    row2 = query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, candidate))
    w = (row2["weight"] or 0.0) if row2 else 0.0
    return w / tot

# =========================
# Parsing do canal
# =========================
MUST_HAVE = (r"ENTRADA\s+CONFIRMADA", r"Mesa:\s*Fantan\s*-\s*Evolution")
MUST_NOT  = (r"\bANALISANDO\b", r"\bPlacar do dia\b", r"\bAPOSTA ENCERRADA\b")

def is_real_entry(text: str) -> bool:
    t = re.sub(r"\s+", " ", text).strip()
    for bad in MUST_NOT:
        if re.search(bad, t, flags=re.I): return False
    for good in MUST_HAVE:
        if not re.search(good, t, flags=re.I): return False
    has_ctx = any(re.search(p, t, flags=re.I) for p in [
        r"Sequ[eê]ncia:\s*[\d\s\|\-]+",
        r"\bKWOK\s*[1-4]\s*-\s*[1-4]",
        r"\bSS?H\s*[1-4](?:-[1-4]){0,3}",
        r"\bODD\b|\bEVEN\b",
        r"Entrar\s+ap[oó]s\s+o\s+[1-4]"
    ])
    return bool(has_ctx)

def extract_seq_raw(text: str) -> Optional[str]:
    m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", text, flags=re.I)
    return m.group(1).strip() if m else None

# Termos flexíveis para GREEN/LOSS/RED (aceita variações) — exigiremos número junto
GREEN_WORD = r"GR+E+E?N"   # GREEN, GREEM, GREN
LOSS_WORD  = r"LO+S+S?"    # LOSS, LOS
RED_WORD   = r"RED"

# Padrões (aceitam com/sem parênteses), mas SEM fallback
GREEN_PATTERNS = [
    re.compile(rf"APOSTA\s+ENCERRADA.*?\b{GREEN_WORD}\b.*?\(([^)]*)\)", re.I | re.S),
    re.compile(rf"\b{GREEN_WORD}\b[^0-9]*([1-4](?:\D+[1-4]){{0,2}})", re.I | re.S),  # GREEN 2 | GREEN 2 3
]
RED_PATTERNS = [
    re.compile(rf"APOSTA\s+ENCERRADA.*?\b({LOSS_WORD}|{RED_WORD})\b.*?\(([^)]*)\)", re.I | re.S),
    re.compile(rf"\b({LOSS_WORD}|{RED_WORD})\b[^0-9]*([1-4](?:\D+[1-4]){{0,2}})", re.I | re.S),  # LOS 1 3 4
]

def extract_outcome_numbers(text:str) -> List[int]:
    """Retorna até 3 números (1..4) SOMENTE se estiverem atrelados à mensagem GREEN/LOS/RED."""
    t = re.sub(r"\s+", " ", text)
    for rx in GREEN_PATTERNS + RED_PATTERNS:
        m = rx.search(t)
        if not m:
            continue
        buf = []
        for g in m.groups():
            if not g:
                continue
            buf.extend(re.findall(r"[1-4]", g))
        nums = [int(x) for x in buf if x in {"1","2","3","4"}]
        if nums:
            return nums[:3]
    return []  # sem números de conferência → não fecha

# =========================
# Sugerir número (tiro seco minimalista)
# =========================
def suggest_number() -> Tuple[Optional[int], float, int]:
    tail = get_tail(WINDOW)
    samples = len(tail)
    if samples < MIN_SAMPLES:
        return None, 0.0, samples
    scores: Dict[int,float] = {1:1e-6,2:1e-6,3:1e-6,4:1e-6}
    if len(tail) >= 2:
        p2_ctx = tail[-2:]
        for c in (1,2,3,4):
            scores[c] += 0.7 * prob_from_ngrams(p2_ctx[:-1], c)
    if len(tail) >= 1:
        p1_ctx = tail[-1:]
        for c in (1,2,3,4):
            scores[c] += 0.3 * prob_from_ngrams(p1_ctx[:-1], c)
    tot = sum(scores.values())
    post = {k: v/tot for k,v in scores.items()} if tot>0 else {k:0.25 for k in (1,2,3,4)}
    best = max(post.items(), key=lambda kv: kv[1])[0]
    conf = post[best]
    if conf < MIN_CONF:
        return None, conf, samples
    return best, conf, samples

# =========================
# Pendências (G0 + G1 + G2)
# =========================
def open_pending(suggested:int):
    exec_write("""
        INSERT INTO pending_outcome (created_at, suggested, stage, open, window_left)
        VALUES (?,?,?,?,?)
    """, (now_ts(), int(suggested), 0, 1, MAX_STAGE))

async def apply_closures_with_numbers(numbers: List[int]):
    """Aplica a sequência de números observados (até 3) nas pendências abertas, na ordem FIFO.
       Agora inclui a sequência na mensagem de GREEN/LOSS."""
    if not numbers:
        return
    rows = query_all("""SELECT id, suggested, stage, open, window_left
                        FROM pending_outcome WHERE open=1 ORDER BY id ASC""")
    if not rows:
        return
    r = rows[0]
    pid, suggested, stage, left = r["id"], int(r["suggested"]), int(r["stage"]), int(r["window_left"])
    stage_names = {0:"G0",1:"G1",2:"G2"}
    seq = numbers[:]  # sequência recebida na msg
    for i, obs in enumerate(numbers, start=1):
        if obs == suggested:
            # GREEN
            exec_write("UPDATE pending_outcome SET open=0, window_left=0 WHERE id=?", (pid,))
            await send_green(suggested, stage_names.get(stage, f"G{stage}"), obs, seq)
            return
        # não bateu
        left_now = max(0, left - 1)
        if left_now > 0:
            # avança estágio e continua aberta
            exec_write("UPDATE pending_outcome SET stage=stage+1, window_left=? WHERE id=?", (left_now, pid))
            # se foi o primeiro erro (G0), registra LOSS visual (com sequência)
            if stage == 0:
                await send_loss(suggested, "G0", obs, seq)
            stage += 1
            left = left_now
        else:
            # esgotou (terceiro número) -> fecha silencioso
            exec_write("UPDATE pending_outcome SET open=0, window_left=0 WHERE id=?", (pid,))
            return

# =========================
# Webhook
# =========================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

@app.get("/")
async def root():
    return {"ok": True, "detail": "Guardião — Tiro Seco (lite v3.1) - fechamento rígido + sequência"}

@app.get("/debug/pending")
async def debug_pending():
    rows = query_all("""SELECT id, created_at, suggested, stage, open, window_left
                        FROM pending_outcome ORDER BY id DESC LIMIT 5""")
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "created_at": r["created_at"],
            "suggested": r["suggested"],
            "stage": r["stage"],
            "open": r["open"],
            "window_left": r["window_left"],
        })
    return {"last5": out}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg:
        return {"ok": True}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    t = re.sub(r"\s+", " ", text)

    # 1) Fechamentos (GREEN/RED/LOSS/LOS): ler até 3 números e aplicar — SOMENTE se houver números
    if re.search(rf"\b({GREEN_WORD}|{LOSS_WORD}|{RED_WORD}|APOSTA\s+ENCERRADA)\b", t, flags=re.I):
        nums = extract_outcome_numbers(t)
        if not nums:
            # Sem número de conferência → não fecha, não libera
            return {"ok": True, "ignored_no_numbers": True}
        # Registrar timeline (todos os números lidos)
        for n in nums:
            append_timeline(n)
        update_ngrams()
        await apply_closures_with_numbers(nums)
        return {"ok": True, "closed_with": nums}

    # 2) ANALISANDO -> alimentar timeline (sequências cruas)
    if re.search(r"\bANALISANDO\b", t, flags=re.I):
        seq = extract_seq_raw(t)
        if seq:
            parts = re.findall(r"[1-4]", seq)
            for n in [int(x) for x in parts][::-1]:
                append_timeline(n)
            update_ngrams()
        return {"ok": True, "analise": True}

    # 3) ENTRADA CONFIRMADA -> gerar tiro seco (G0) e abrir pendência
    if is_real_entry(t):
        # bloquear novo sinal se já há pendência aberta
        open_cnt = query_one("SELECT COUNT(*) AS c FROM pending_outcome WHERE open=1")["c"] or 0
        if open_cnt > 0:
            return {"ok": True, "skipped_open_pending": True}
        # evitar duplicado por message_id
        source_msg_id = msg.get("message_id")
        if query_one("SELECT 1 FROM suggestions WHERE source_msg_id=?", (source_msg_id,)):
            return {"ok": True, "dup": True}
        best, conf, samples = suggest_number()
        if best is None:
            return {"ok": True, "skipped_low_conf": True, "samples": samples}
        exec_write("INSERT OR REPLACE INTO suggestions (source_msg_id, suggested_number, sent_at) VALUES (?,?,?)",
                   (source_msg_id, int(best), now_ts()))
        open_pending(int(best))
        await send_sinal_g0(int(best), conf, samples)
        return {"ok": True, "sent": True, "number": int(best), "conf": conf, "samples": samples}

    return {"ok": True, "skipped": True}
