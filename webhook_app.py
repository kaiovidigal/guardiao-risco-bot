 -*- coding: utf-8 -*-
# Guardião — Tiro Seco (lite v3.2 DEBUG)
# >>> Versão com LOG detalhado em stdout (print) para inspeção no Render logs
# >>> Não altera o funcionamento: só adiciona prints nos pontos-chave.
#
# O que loga:
# - Texto recebido (normalizado) e ID da mensagem
# - Branch do webhook seguido: CLOSURE(GREEN/LOSS), ANALISANDO, ENTRADA, SKIPPED
# - Números de conferência extraídos (até 3) e sequência exibida
# - Abertura/fechamento de pendência (id, stage, window_left)
# - Motivo de skip (ex.: "ignored_no_numbers", "skipped_open_pending", "skipped_low_conf")
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

DB_PATH       = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
TG_BOT_TOKEN  = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
REPL_CHANNEL  = os.getenv("REPL_CHANNEL", "").strip() or "-1003052132833"

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

MAX_STAGE   = 3
WINDOW      = 400
MIN_SAMPLES = 120
MIN_CONF    = 0.48

app = FastAPI(title="Guardião — Tiro Seco (lite v3.2 DEBUG)", version="1.3.2")

# =========================
# DB helpers
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
        print(f"[SEND] -> {REPL_CHANNEL}: {text.replace(chr(10),' | ')}")
        await tg_send_text(REPL_CHANNEL, text, parse)

def _seq_str(numbers: List[int]) -> str:
    return " | ".join(str(n) for n in numbers) if numbers else "—"

async def send_green(sugerido:int, stage_txt:str, conferido:int, seq:List[int]):
    msg = (
        f"✅ <b>GREEN</b> (<b>{conferido}</b>) em <b>{stage_txt}</b> — Número: <b>{sugerido}</b> • Conf: <b>{conferido}</b>\n"
        f"🚥 <b>Sequência:</b> {_seq_str(seq)}"
    )
    print(f"[GREEN] sug={sugerido} stage={stage_txt} conferido={conferido} seq={seq}")
    await tg_broadcast(msg)

async def send_loss(sugerido:int, stage_txt:str, conferido:int, seq:List[int]):
    msg = (
        f"❌ <b>LOSS</b> (<b>{conferido}</b>) — Número: <b>{sugerido}</b> ({stage_txt}) • Conf: <b>{conferido}</b>\n"
        f"🚥 <b>Sequência:</b> {_seq_str(seq)}"
    )
    print(f"[LOSS] sug={sugerido} stage={stage_txt} conferido={conferido} seq={seq}")
    await tg_broadcast(msg)

async def send_sinal_g0(num:int, conf:float, samples:int):
    msg = (
        f"🤖 <b>Tiro seco</b>\n"
        f"🎯 Número seco (G0): <b>{num}</b>\n"
        f"📈 Conf: <b>{conf*100:.1f}%</b> | Amostra≈<b>{samples}</b>"
    )
    print(f"[SIGNAL] G0 num={num} conf={conf:.3f} samples={samples}")
    await tg_broadcast(msg)

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
        nxt = tail[t]; dist = (len(tail)-1) - t; w = (decay ** dist)
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

GREEN_WORD = r"GR+E+E?N"
LOSS_WORD  = r"LO+S+S?"
RED_WORD   = r"RED"

GREEN_PATTERNS = [
    re.compile(rf"APOSTA\s+ENCERRADA.*?\b{GREEN_WORD}\b.*?\(([^)]*)\)", re.I | re.S),
    re.compile(rf"\b{GREEN_WORD}\b[^0-9]*([1-4](?:\D+[1-4]){{0,2}})", re.I | re.S),
]
RED_PATTERNS = [
    re.compile(rf"APOSTA\s+ENCERRADA.*?\b({LOSS_WORD}|{RED_WORD})\b.*?\(([^)]*)\)", re.I | re.S),
    re.compile(rf"\b({LOSS_WORD}|{RED_WORD})\b[^0-9]*([1-4](?:\D+[1-4]){{0,2}})", re.I | re.S),
]

def extract_outcome_numbers(text:str) -> List[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in GREEN_PATTERNS + RED_PATTERNS:
        m = rx.search(t)
        if not m: continue
        buf = []
        for g in m.groups():
            if not g: continue
            buf.extend(re.findall(r"[1-4]", g))
        nums = [int(x) for x in buf if x in {"1","2","3","4"}]
        if nums:
            return nums[:3]
    return []

# =========================
# Sugerir número
# =========================
def suggest_number() -> Tuple[Optional[int], float, int]:
    tail = get_tail(WINDOW)
    samples = len(tail)
    if samples < MIN_SAMPLES:
        print(f"[SUGGEST] amostras insuficientes ({samples}<{MIN_SAMPLES})")
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
    print(f"[SUGGEST] best={best} conf={conf:.3f} samples={samples}")
    if conf < MIN_CONF:
        print(f"[SUGGEST] conf abaixo do mínimo ({conf:.3f}<{MIN_CONF:.3f})")
        return None, conf, samples
    return best, conf, samples

# =========================
# Pendências
# =========================
def open_pending(suggested:int):
    exec_write("""
        INSERT INTO pending_outcome (created_at, suggested, stage, open, window_left)
        VALUES (?,?,?,?,?)
    """, (now_ts(), int(suggested), 0, 1, MAX_STAGE))
    row = query_one("SELECT id FROM pending_outcome ORDER BY id DESC LIMIT 1")
    print(f"[PENDING] opened id={row['id']} sug={suggested} stage=0 left={MAX_STAGE}")

async def apply_closures_with_numbers(numbers: List[int]):
    if not numbers:
        return
    rows = query_all("""SELECT id, suggested, stage, open, window_left
                        FROM pending_outcome WHERE open=1 ORDER BY id ASC""")
    if not rows:
        print("[CLOSE] não há pendências abertas")
        return
    r = rows[0]
    pid, suggested, stage, left = r["id"], int(r["suggested"]), int(r["stage"]), int(r["window_left"])
    stage_names = {0:"G0",1:"G1",2:"G2"}
    seq = numbers[:]
    print(f"[CLOSE] pid={pid} sug={suggested} stage={stage} left={left} nums={numbers}")
    for i, obs in enumerate(numbers, start=1):
        if obs == suggested:
            exec_write("UPDATE pending_outcome SET open=0, window_left=0 WHERE id=?", (pid,))
            print(f"[CLOSE] -> GREEN pid={pid} conferido={obs}")
            await send_green(suggested, stage_names.get(stage, f"G{stage}"), obs, seq)
            return
        left_now = max(0, left - 1)
        if left_now > 0:
            exec_write("UPDATE pending_outcome SET stage=stage+1, window_left=? WHERE id=?", (left_now, pid))
            print(f"[CLOSE] avanço de estágio pid={pid} -> stage={stage+1} left={left_now} (obs={obs})")
            if stage == 0:
                await send_loss(suggested, "G0", obs, seq)
            stage += 1
            left = left_now
        else:
            exec_write("UPDATE pending_outcome SET open=0, window_left=0 WHERE id=?", (pid,))
            print(f"[CLOSE] esgotou janela pid={pid} (obs final={obs})")
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
    return {"ok": True, "detail": "Guardião — Tiro Seco (lite v3.2 DEBUG)"}

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
        print("[WEBHOOK] vazio")
        return {"ok": True}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    t = re.sub(r"\s+", " ", text)
    mid = msg.get("message_id")
    print(f"[WEBHOOK] msg_id={mid} text='{t}'")

    # 1) CLOSURES
    if re.search(rf"\b({GREEN_WORD}|{LOSS_WORD}|{RED_WORD}|APOSTA\s+ENCERRADA)\b", t, flags=re.I):
        nums = extract_outcome_numbers(t)
        print(f"[BRANCH] CLOSURE nums={nums}")
        if not nums:
            print("[SKIP] ignored_no_numbers (fechamento sem números)")
            return {"ok": True, "ignored_no_numbers": True}
        for n in nums:
            append_timeline(n)
        update_ngrams()
        await apply_closures_with_numbers(nums)
        return {"ok": True, "closed_with": nums}

    # 2) ANALISANDO
    if re.search(r"\bANALISANDO\b", t, flags=re.I):
        seq = extract_seq_raw(t)
        print(f"[BRANCH] ANALISANDO seq_raw='{seq}'")
        if seq:
            parts = re.findall(r"[1-4]", seq)
            for n in [int(x) for x in parts][::-1]:
                append_timeline(n)
            update_ngrams()
        return {"ok": True, "analise": True}

    # 3) ENTRADA
    if is_real_entry(t):
        open_cnt = query_one("SELECT COUNT(*) AS c FROM pending_outcome WHERE open=1")["c"] or 0
        print(f"[BRANCH] ENTRADA open_cnt={open_cnt}")
        if open_cnt > 0:
            print("[SKIP] skipped_open_pending")
            return {"ok": True, "skipped_open_pending": True}
        source_msg_id = msg.get("message_id")
        if query_one("SELECT 1 FROM suggestions WHERE source_msg_id=?", (source_msg_id,)):
            print("[SKIP] dup")
            return {"ok": True, "dup": True}
        best, conf, samples = suggest_number()
        if best is None:
            print("[SKIP] skipped_low_conf")
            return {"ok": True, "skipped_low_conf": True, "samples": samples}
        exec_write("INSERT OR REPLACE INTO suggestions (source_msg_id, suggested_number, sent_at) VALUES (?,?,?)",
                   (source_msg_id, int(best), now_ts()))
        open_pending(int(best))
        await send_sinal_g0(int(best), conf, samples)
        return {"ok": True, "sent": True, "number": int(best), "conf": conf, "samples": samples}

    print("[BRANCH] SKIPPED (não casou com nenhum parser)")
    return {"ok": True, "skipped": True}
