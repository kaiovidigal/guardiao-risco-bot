#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webhook_app.py — Guardião FanTan (Fluxo 100% + QUALI 79)
--------------------------------------------------------
• Publica toda ENTRADA CONFIRMADA na hora (G0).
• Acompanha G1/G2 e fecha com GREEN/LOSS visível.
• Camada de "inteligência" marca [QUALI] (subset com meta ~79%),
  mas NUNCA bloqueia o fluxo.
• Placar diário geral + placar diário do subset QUALI.

ENV obrigatórias:
  TG_BOT_TOKEN       : token do bot (ex.: 1234:ABC)
  WEBHOOK_TOKEN      : segredo do endpoint (ex.: meusegredo123)
  PUBLIC_CHANNEL     : chat_id destino (ex.: -100xxxxxxxxxx)

ENV opcionais:
  DB_PATH            : caminho SQLite (default /var/data/data.db)
  FLUSH_KEY          : chave p/ /debug/flush (default meusegredo123)

QUALI (subset de alta assertividade) — ajustáveis via ENV:
  QUALI_ENABLED      : 1 liga badges/placar (default 1)
  QUALI_MIN_SAMPLES  : amostras mínimas n-gram (default 800)
  QUALI_MIN_CONF     : confiança mínima (default 0.66)
  QUALI_MIN_GAP      : gap top1-top2 mínimo (default 0.10)
  QUALI_TOPK_FREQ    : 1 = top1 | 2 = top1 ou top2 (default 2)
  QUALI_WINDOW       : janela da cauda (default 50)

Webhook:
  POST /webhook/{WEBHOOK_TOKEN}
Health:
  GET  /health
Debug:
  GET  /debug/state
  GET  /debug/flush?key=<FLUSH_KEY>
"""

import os, re, json, time, sqlite3, asyncio
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone, timedelta
from collections import Counter

import httpx
from fastapi import FastAPI, Request, HTTPException

# =========================
# ENV / CONFIG
# =========================
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN or not PUBLIC_CHANNEL:
    raise RuntimeError("Defina TG_BOT_TOKEN, WEBHOOK_TOKEN e PUBLIC_CHANNEL nas variáveis de ambiente.")

DB_PATH  = os.getenv("DB_PATH", "/var/data/data.db").strip() or "/var/data/data.db"
FLUSH_KEY= os.getenv("FLUSH_KEY", "meusegredo123").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# QUALI subset (meta ~79%)
QUALI_ENABLED       = int(os.getenv("QUALI_ENABLED", "1"))
QUALI_MIN_SAMPLES   = int(os.getenv("QUALI_MIN_SAMPLES", "800"))
QUALI_MIN_CONF      = float(os.getenv("QUALI_MIN_CONF", "0.66"))
QUALI_MIN_GAP       = float(os.getenv("QUALI_MIN_GAP",  "0.10"))
QUALI_TOPK_FREQ     = int(os.getenv("QUALI_TOPK_FREQ",  "2"))
QUALI_WINDOW        = int(os.getenv("QUALI_WINDOW",     "50"))

# Modelo simples (n-gram backoff)
WINDOW = 400       # cauda máxima
DECAY  = 0.985
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12
ALPHA, BETA = 1.05, 1.00      # ALPHA dá peso ao n-gram; BETA reserva p/ futuros sinais auxiliares
GAP_MIN_DISPLAY = 0.015       # Apenas para exibição (não bloqueia)

# Gale/pendência
MAX_STAGE = 3  # G0,G1,G2

app = FastAPI(title="Guardião FanTan — Fluxo 100% + QUALI", version="1.0.0")

# =========================
# SQLite helpers
# =========================
def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=10000;")
    return con

def exec_write(sql: str, params: tuple = (), retries: int = 6, wait: float = 0.25):
    for _ in range(retries):
        try:
            con = _connect()
            con.execute(sql, params)
            con.commit()
            con.close()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(wait); continue
            raise
    raise sqlite3.OperationalError("Banco bloqueado após várias tentativas.")

def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    con = _connect()
    row = con.execute(sql, params).fetchone()
    con.close()
    return row

def query_all(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    con = _connect()
    rows = con.execute(sql, params).fetchall()
    con.close()
    return rows

def _column_exists(con: sqlite3.Connection, table: str, col: str) -> bool:
    r = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any((row["name"] if isinstance(row, sqlite3.Row) else row[1]) == col for row in r)

# =========================
# Init DB + migração
# =========================
def init_db():
    con = _connect(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram_stats (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, nxt INTEGER NOT NULL, w REAL NOT NULL,
        PRIMARY KEY (n, ctx, nxt)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_outcome (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        suggested INTEGER NOT NULL,
        stage INTEGER NOT NULL,
        open INTEGER NOT NULL,
        window_left INTEGER NOT NULL,
        seen_numbers TEXT DEFAULT '',
        is_quali INTEGER NOT NULL DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY,
        g0 INTEGER NOT NULL DEFAULT 0,
        g1 INTEGER NOT NULL DEFAULT 0,
        g2 INTEGER NOT NULL DEFAULT 0,
        loss INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score_quali (
        yyyymmdd TEXT PRIMARY KEY,
        green INTEGER NOT NULL DEFAULT 0,
        loss  INTEGER NOT NULL DEFAULT 0
    )""")
    con.commit()

    # migração idempotente (colunas futuras)
    try:
        if not _column_exists(con, "pending_outcome", "is_quali"):
            cur.execute("ALTER TABLE pending_outcome ADD COLUMN is_quali INTEGER NOT NULL DEFAULT 0")
            con.commit()
    except sqlite3.OperationalError:
        pass
    con.close()

init_db()

# =========================
# Utils
# =========================
def now_ts() -> int:
    return int(time.time())

def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")

def get_recent_tail(window:int = WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [int(r["number"]) for r in rows][::-1]

def append_timeline(n:int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def update_ngrams(decay:float=DECAY, max_n:int=5, window:int=WINDOW):
    tail = get_recent_tail(window)
    if len(tail) < 2: return
    for t in range(1, len(tail)):
        nxt = int(tail[t])
        dist = (len(tail)-1) - t
        w = (decay ** dist)
        for n in range(2, max_n+1):
            if t-(n-1) < 0: break
            ctx = tail[t-(n-1):t]
            ctx_key = ",".join(str(x) for x in ctx)
            exec_write("""
                INSERT INTO ngram_stats (n, ctx, nxt, w)
                VALUES (?,?,?,?)
                ON CONFLICT(n, ctx, nxt) DO UPDATE SET w = w + excluded.w
            """, (n, ctx_key, nxt, float(w)))

def prob_from_ngrams(ctx: List[int], cand: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    row = query_one("SELECT SUM(w) AS s FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key))
    tot = float(row["s"] or 0.0) if row else 0.0
    if tot <= 0: return 0.0
    row2 = query_one("SELECT w FROM ngram_stats WHERE n=? AND ctx=? AND nxt=?", (n, ctx_key, int(cand)))
    w = float(row2["w"] or 0.0) if row2 else 0.0
    return w / tot if tot > 0 else 0.0

def ngram_backoff_score(tail: List[int], after_num: Optional[int], cand: int) -> float:
    # contexto flexível: último, 2 últimos, etc. ou após um número específico
    if not tail: return 0.0
    if after_num is not None and after_num in tail:
        i = [idx for idx,v in enumerate(tail) if v==after_num][-1]
        ctx1 = tail[max(0,i):i+1]
        ctx2 = tail[max(0,i-1):i+1] if i-1>=0 else []
        ctx3 = tail[max(0,i-2):i+1] if i-2>=0 else []
        ctx4 = tail[max(0,i-3):i+1] if i-3>=0 else []
    else:
        ctx4 = tail[-4:] if len(tail)>=4 else []
        ctx3 = tail[-3:] if len(tail)>=3 else []
        ctx2 = tail[-2:] if len(tail)>=2 else []
        ctx1 = tail[-1:] if len(tail)>=1 else []

    score = 0.0
    if len(ctx4)==4: score += W4 * prob_from_ngrams(ctx4[:-1], cand)
    if len(ctx3)==3: score += W3 * prob_from_ngrams(ctx3[:-1], cand)
    if len(ctx2)==2: score += W2 * prob_from_ngrams(ctx2[:-1], cand)
    if len(ctx1)==1: score += W1 * prob_from_ngrams(ctx1[:-1], cand)
    return score

# =========================
# QUALI helpers (meta 79%)
# =========================
def _topk_recent_tail(tail: List[int], k:int, win:int) -> List[int]:
    if not tail: return []
    arr = tail[-win:] if len(tail) >= win else tail[:]
    return [n for (n,_) in Counter(arr).most_common(k)]

def qualify_79(conf: float, gap: float, tail: List[int], best: int, samples: int) -> bool:
    if not QUALI_ENABLED: return False
    if samples < QUALI_MIN_SAMPLES: return False
    if conf < QUALI_MIN_CONF or gap < QUALI_MIN_GAP: return False
    topk = _topk_recent_tail(tail, QUALI_TOPK_FREQ, QUALI_WINDOW)
    return best in topk

def _bump_quali(won: bool):
    y = today_key()
    row = query_one("SELECT green,loss FROM daily_score_quali WHERE yyyymmdd=?", (y,))
    g = (int(row["green"]) if row else 0) + (1 if won else 0)
    l = (int(row["loss"])  if row else 0) + (0 if won else 1)
    exec_write("""INSERT OR REPLACE INTO daily_score_quali (yyyymmdd,green,loss)
                  VALUES (?,?,?)""", (y,g,l))

def _quali_line() -> str:
    y = today_key()
    row = query_one("SELECT green,loss FROM daily_score_quali WHERE yyyymmdd=?", (y,))
    g = int(row["green"]) if row else 0
    l = int(row["loss"]) if row else 0
    acc = (g/(g+l)*100.0) if (g+l)>0 else 0.0
    return f"⚙️ QUALI79: {g} GREEN × {l} LOSS — {acc:.1f}%"

# =========================
# Placar geral
# =========================
def update_daily_score(stage: int, won: bool):
    y = today_key()
    r = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0 = int(r["g0"]) if r else 0
    g1 = int(r["g1"]) if r else 0
    g2 = int(r["g2"]) if r else 0
    loss = int(r["loss"]) if r else 0
    streak = int(r["streak"]) if r else 0

    if won:
        if stage == 0: g0 += 1
        elif stage == 1: g1 += 1
        elif stage == 2: g2 += 1
        streak += 1
    else:
        loss += 1
        streak = 0

    exec_write("""INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
                  VALUES (?,?,?,?,?,?)""", (y,g0,g1,g2,loss,streak))
    total = g0 + g1 + g2 + loss
    acc = ((g0+g1+g2)/total*100.0) if total else 0.0
    return g0,g1,g2,loss,acc,streak

# =========================
# Telegram
# =========================
async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse,
                                "disable_web_page_preview": True})

# =========================
# Parsers do canal-fonte
# =========================
MUST_HAVE = (r"ENTRADA\s+CONFIRMADA", r"Mesa:\s*Fantan\s*-\s*Evolution")
MUST_NOT  = (r"\bANALISANDO\b", r"\bPlacar do dia\b")

def is_real_entry(text: str) -> bool:
    t = re.sub(r"\s+", " ", text).strip()
    for bad in MUST_NOT:
        if re.search(bad, t, flags=re.I): return False
    for good in MUST_HAVE:
        if not re.search(good, t, flags=re.I): return False
    return True

def extract_after_num(text: str) -> Optional[int]:
    m = re.search(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", text, flags=re.I)
    return int(m.group(1)) if m else None

# Fechamentos (muitos formatos)
GREEN_PATTERNS = [
    re.compile(r"\bGREEN\b.*?\(([1-4])\)", re.I | re.S),
    re.compile(r"✅.*?\(([1-4])\)", re.I | re.S),
    re.compile(r"\bWIN\b.*?\(([1-4])\)", re.I | re.S),
]
RED_PATTERNS = [
    re.compile(r"\bRED\b.*?\(([1-4])\)", re.I | re.S),
    re.compile(r"\bLOSS\b.*?\(([1-4])\)", re.I | re.S),
    re.compile(r"❌.*?\(([1-4])\)", re.I | re.S),
]

def extract_green_number(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in GREEN_PATTERNS:
        m = rx.search(t)
        if m:
            nums = re.findall(r"[1-4]", m.group(1))
            if nums: return int(nums[0])
    return None

def extract_red_number(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in RED_PATTERNS:
        m = rx.search(t)
        if m:
            nums = re.findall(r"[1-4]", m.group(1))
            if nums: return int(nums[0])
    return None

# =========================
# Sugerir número (sempre retorna algo; NÃO bloqueia fluxo)
# =========================
def suggest_number(after_num: Optional[int]) -> Tuple[int, float, int, Dict[int,float], float]:
    base = [1,2,3,4]
    tail = get_recent_tail(WINDOW)

    # n-gram backoff + prior uniforme
    scores: Dict[int,float] = {}
    for c in base:
        ng = ngram_backoff_score(tail, after_num, c)
        prior = 1.0/len(base)
        scores[c] = (prior) * ((ng or 1e-6) ** ALPHA)

    total = sum(scores.values()) or 1e-9
    post = {k: v/total for k,v in scores.items()}
    top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not top:
        return 1, 0.25, len(tail), {1:0.25,2:0.25,3:0.25,4:0.25}, 0.0

    best = int(top[0][0])
    conf = float(top[0][1])
    gap  = float(top[0][1] - (top[1][1] if len(top)>1 else 0.0))
    samples_row = query_one("SELECT SUM(w) AS s FROM ngram_stats")
    samples = int((samples_row["s"] or 0) if samples_row else 0)
    return best, conf, samples, post, gap

# =========================
# Pendências (G0/G1/G2)
# =========================
def open_pending(suggested: int, is_quali: bool):
    exec_write("""INSERT INTO pending_outcome
                  (created_at, suggested, stage, open, window_left, seen_numbers, is_quali)
                  VALUES (?,?,?,?,?,?,?)""",
               (now_ts(), int(suggested), 0, 1, MAX_STAGE, "", 1 if is_quali else 0))

def _append_seen(pid:int, seen_csv:str, new:int) -> str:
    arr = [int(x) for x in seen_csv.split(",") if x] if seen_csv else []
    arr.append(int(new))
    return ",".join(str(x) for x in arr[-3:])

async def _announce_gale(stage_now:int):
    if stage_now == 1:
        await tg_send_text(PUBLIC_CHANNEL, "🔁 Estamos no <b>1° gale (G1)</b>")
    elif stage_now == 2:
        await tg_send_text(PUBLIC_CHANNEL, "🔁 Estamos no <b>2° gale (G2)</b>")

async def close_pending_with_observed(n_observed:int):
    rows = query_all("""SELECT id, suggested, stage, open, window_left, seen_numbers, is_quali
                        FROM pending_outcome WHERE open=1 ORDER BY id ASC""")
    if not rows: return
    for r in rows:
        pid  = int(r["id"])
        sug  = int(r["suggested"])
        stg  = int(r["stage"])
        left = int(r["window_left"])
        seen = r["seen_numbers"] or ""
        is_quali = bool(r["is_quali"])

        seen_new = _append_seen(pid, seen, n_observed)

        if n_observed == sug:
            # GREEN
            exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                       (seen_new, pid))
            g0,g1,g2,loss,acc,streak = update_daily_score(stg, True)
            if is_quali:
                _bump_quali(True)

            seq_txt = " | ".join([str(x) for x in [int(x) for x in seen_new.split(',') if x]])
            await tg_send_text(
                PUBLIC_CHANNEL,
                f"✅ <b>GREEN</b> em <b>{['G0','G1','G2'][stg]}</b>\n"
                f"🚥 Sequência: {seq_txt}\n"
                f"📊 Placar do dia: G0={g0} G1={g1} G2={g2} LOSS={loss} — {acc:.2f}%\n"
                f"{_quali_line()}"
            )
        else:
            # não bateu
            if left > 1:
                # avança para próximo gale
                exec_write("""UPDATE pending_outcome
                              SET stage=stage+1, window_left=window_left-1, seen_numbers=?
                              WHERE id=?""", (seen_new, pid))
                await _announce_gale(stg+1)
            else:
                # LOSS final
                exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                           (seen_new, pid))
                g0,g1,g2,loss,acc,streak = update_daily_score(stg, False)
                if is_quali:
                    _bump_quali(False)

                seq_txt = " | ".join([str(x) for x in [int(x) for x in seen_new.split(',') if x]])
                await tg_send_text(
                    PUBLIC_CHANNEL,
                    f"❌ <b>LOSS</b> (em <b>{['G0','G1','G2'][stg]}</b>)\n"
                    f"🚥 Sequência: {seq_txt}\n"
                    f"📊 Placar do dia: G0={g0} G1={g1} G2={g2} LOSS={loss} — {acc:.2f}%\n"
                    f"{_quali_line()}"
                )

# =========================
# Routes
# =========================
@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>"}

@app.get("/health")
async def health():
    y = today_key()
    r = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0 = int(r["g0"]) if r else 0
    g1 = int(r["g1"]) if r else 0
    g2 = int(r["g2"]) if r else 0
    loss = int(r["loss"]) if r else 0
    streak = int(r["streak"]) if r else 0
    rowq = query_one("SELECT green,loss FROM daily_score_quali WHERE yyyymmdd=?", (y,))
    qg = int(rowq["green"]) if rowq else 0
    ql = int(rowq["loss"]) if rowq else 0
    return {
        "ok": True,
        "db": DB_PATH,
        "utc": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
        "score_today": {"g0":g0, "g1":g1, "g2":g2, "loss":loss, "streak":streak},
        "quali_today": {"green":qg, "loss":ql}
    }

@app.get("/debug/state")
async def debug_state():
    row_ng = query_one("SELECT SUM(w) AS s FROM ngram_stats")
    samples = int((row_ng["s"] or 0) if row_ng else 0)
    pend_open = int((query_one("SELECT COUNT(*) AS c FROM pending_outcome WHERE open=1") or {"c":0})["c"])
    y = today_key()
    r = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0 = int(r["g0"]) if r else 0
    g1 = int(r["g1"]) if r else 0
    g2 = int(r["g2"]) if r else 0
    loss = int(r["loss"]) if r else 0
    streak = int(r["streak"]) if r else 0
    rq = query_one("SELECT green,loss FROM daily_score_quali WHERE yyyymmdd=?", (y,))
    qg = int(rq["green"]) if rq else 0
    ql = int(rq["loss"]) if rq else 0
    acc = ((g0+g1+g2)/(g0+g1+g2+loss)) if (g0+g1+g2+loss)>0 else 0.0
    qacc= (qg/(qg+ql)) if (qg+ql)>0 else 0.0
    return {
        "samples": samples,
        "pending_open": pend_open,
        "score_today": {"g0":g0, "g1":g1, "g2":g2, "loss":loss, "acc": round(acc*100,2), "streak":streak},
        "quali_today": {"green":qg, "loss":ql, "acc": round(qacc*100,2)},
    }

@app.get("/debug/flush")
async def debug_flush(key: str = "", days: int = 7):
    if not key or key != FLUSH_KEY:
        return {"ok": False, "error": "unauthorized"}
    # Snapshot simples: só manda placares
    y = today_key()
    r = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0 = int(r["g0"]) if r else 0
    g1 = int(r["g1"]) if r else 0
    g2 = int(r["g2"]) if r else 0
    loss = int(r["loss"]) if r else 0
    total = g0+g1+g2+loss
    acc = ((g0+g1+g2)/total*100.0) if total else 0.0

    rq = query_one("SELECT green,loss FROM daily_score_quali WHERE yyyymmdd=?", (y,))
    qg = int(rq["green"]) if rq else 0
    ql = int(rq["loss"]) if rq else 0
    qtot = qg+ql
    qacc = (qg/qtot*100.0) if qtot else 0.0

    txt = (f"📊 <b>Placar do dia</b>\n"
           f"🟢 G0:{g0}  🟢 G1:{g1}  🟢 G2:{g2}  🔴 LOSS:{loss}\n"
           f"✅ Acerto: {acc:.2f}%\n"
           f"{_quali_line()}")
    await tg_send_text(PUBLIC_CHANNEL, txt)
    return {"ok": True, "sent": True, "acc_today": acc, "quali_acc_today": qacc}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    msg = data.get("channel_post") or data.get("message") \
        or data.get("edited_channel_post") or data.get("edited_message") or {}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return {"ok": True, "skipped": "sem_texto"}

    # 1) GREEN / RED: fecha pendências e alimenta timeline
    gnum = extract_green_number(text)
    rnum = extract_red_number(text)
    if gnum is not None or rnum is not None:
        n = int(gnum if gnum is not None else rnum)
        append_timeline(n)
        update_ngrams()
        await close_pending_with_observed(n)
        return {"ok": True, "observed": n}

    # 2) ENTRADA CONFIRMADA: publica G0 SEM travar + marca QUALI
    if is_real_entry(text):
        after = extract_after_num(text)
        best, conf, samples, post, gap = suggest_number(after)

        # marca QUALI (subset), mas NÃO bloqueia o fluxo
        tail = get_recent_tail(WINDOW)
        is_quali = qualify_79(conf, gap, tail, int(best), int(samples))

        open_pending(int(best), is_quali)
        aft_txt = f" após {after}" if after else ""
        badge = " 🟩<b>[QUALI]</b>" if is_quali else ""
        await tg_send_text(
            PUBLIC_CHANNEL,
            f"🎯 <b>Entrada Confirmada</b>{badge}\n"
            f"⚡ Número seco (G0): <b>{best}</b>\n"
            f"🧩 Padrão: GEN{aft_txt}\n"
            f"📊 Conf: {conf*100:.2f}% | Gap: {gap*100:.2f}% | Amostra≈{samples}\n"
            f"{_quali_line()}"
        )
        return {"ok": True, "sent": True, "number": int(best), "conf": float(conf), "gap": float(gap), "samples": int(samples)}

    # 3) Mensagens de "analisando" / neutras: tentar extrair sequência e alimentar memória
    if re.search(r"\bANALISANDO\b", re.sub(r"\s+", " ", text), flags=re.I):
        # se tiver sequência “1 | 3 | 4” em análise, adiciona ao timeline (esquerda→direita)
        m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", text, flags=re.I)
        if m:
            parts = re.findall(r"[1-4]", m.group(1))
            seq = [int(x) for x in parts]
            # adiciona da esquerda para a direita
            for n in seq:
                append_timeline(n)
            update_ngrams()
        return {"ok": True, "analise": True}

    return {"ok": True, "skipped": True}