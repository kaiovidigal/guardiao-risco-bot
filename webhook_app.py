#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webhook_app.py — modo G0 (sem gales) — v5.0
- Sugere e fecha no PRIMEIRO número observado (G0-only)
- Ignora mensagens de gale (G1/G2...)
- Mantém: dedupe por update_id, filtro de origem, reset diário, CHAIN_ON, n-gram + feedback, ensemble básico
- Tokens EMBUTIDOS (sem depender de ENV)

Start (Render/UVicorn):
  uvicorn webhook_app:app --host 0.0.0.0 --port $PORT
"""

import os, re, time, sqlite3, math, json
from contextlib import contextmanager
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= CONFIG FIXA (EDITAR AQUI SE PRECISAR) =========
TG_BOT_TOKEN   = "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4"
WEBHOOK_TOKEN  = "meusegredo123"
TARGET_CHANNEL = "-1002796105884"     # destino
SOURCE_CHANNEL = "-1002810508717"     # fonte (monitorado)
DB_PATH        = "/data/data.db"      # Render disk default (/data)
TZ_NAME        = "America/Sao_Paulo"
BYPASS_SOURCE  = False                # True = ignora filtro de origem
DEBUG_MSG      = False                # mensagens de diagnóstico no TARGET

# CHAIN_ON: após fechar, já abre um novo
CHAIN_ON       = True

# ========= TIMEZONE =========
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(TZ_NAME)
except Exception:
    _TZ = timezone.utc

# ========= Parâmetros do motor =========
DECAY = 0.980
W4, W3, W2, W1 = 0.42, 0.30, 0.18, 0.10
OBS_TIMEOUT_SEC = 120  # G0: se não veio NENHUM observado nesse tempo, fecha LOSS por timeout

# Hedge
HEDGE_ETA = 0.6
K_SHORT   = 60
K_LONG    = 300

# Online Learning
FEED_BETA   = 0.40
FEED_POS    = 0.60
FEED_NEG    = 1.20
FEED_DECAY  = 0.995
WF4, WF3, WF2, WF1 = W4, W3, W2, W1

# API do Telegram
TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ========= Utils =========
def now_ts() -> int:
    return int(time.time())

def ts_str(ts=None) -> str:
    if ts is None: ts = now_ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def tz_today_ymd() -> str:
    dt = datetime.now(_TZ)
    return dt.strftime("%Y-%m-%d")

def _norm_dict(d: Dict[int,float]) -> Dict[int,float]:
    s = sum(d.values()) or 1e-9
    return {k: v/s for k,v in d.items()}

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

def _ensure_column(con: sqlite3.Connection, table: str, col: str, decl: str):
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    names = {(r["name"] if isinstance(r, sqlite3.Row) else r[1]) for r in rows}
    if col not in names:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

def migrate_db():
    con = _connect(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, nxt INTEGER NOT NULL, w REAL NOT NULL,
        PRIMARY KEY (n, ctx, nxt)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER,
        suggested INTEGER,
        stage INTEGER DEFAULT 0,
        open INTEGER DEFAULT 1,
        seen TEXT,
        opened_at INTEGER,
        after INTEGER,
        ctx1 TEXT, ctx2 TEXT, ctx3 TEXT, ctx4 TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS score (
        id INTEGER PRIMARY KEY CHECK (id=1),
        green INTEGER DEFAULT 0,
        loss  INTEGER DEFAULT 0
    )""")
    if not con.execute("SELECT 1 FROM score WHERE id=1").fetchone():
        cur.execute("INSERT INTO score (id, green, loss) VALUES (1,0,0)")
    cur.execute("""CREATE TABLE IF NOT EXISTS feedback (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, nxt INTEGER NOT NULL, w REAL NOT NULL,
        PRIMARY KEY (n, ctx, nxt)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS processed (
        update_id TEXT PRIMARY KEY,
        seen_at   INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS state (
        id INTEGER PRIMARY KEY CHECK (id=1),
        loss_streak   INTEGER DEFAULT 0,
        last_reset_ymd TEXT DEFAULT ''
    )""")
    if not con.execute("SELECT 1 FROM state WHERE id=1").fetchone():
        cur.execute("INSERT INTO state (id, loss_streak, last_reset_ymd) VALUES (1,0,'')")

    cur.execute("""CREATE TABLE IF NOT EXISTS expert_w (
        id INTEGER PRIMARY KEY CHECK (id=1),
        w1 REAL NOT NULL, w2 REAL NOT NULL, w3 REAL NOT NULL, w4 REAL NOT NULL
    )""")
    if not con.execute("SELECT 1 FROM expert_w WHERE id=1").fetchone():
        cur.execute("INSERT INTO expert_w (id, w1, w2, w3, w4) VALUES (1,1.0,1.0,1.0,1.0)")
    con.commit(); con.close()
migrate_db()

def _exec_write(sql: str, params: tuple=()):
    for attempt in range(5):
        try:
            with _tx() as con:
                con.execute(sql, params)
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(0.25*(attempt+1)); continue
            raise

# ========= Score/state =========
def bump_score(outcome: str) -> Tuple[int, int]:
    with _tx() as con:
        g, l = (0, 0)
        row = con.execute("SELECT green, loss FROM score WHERE id=1").fetchone()
        if row: g, l = int(row["green"] or 0), int(row["loss"] or 0)
        if outcome.upper() == "GREEN": g += 1
        elif outcome.upper() == "LOSS": l += 1
        con.execute("INSERT OR REPLACE INTO score (id, green, loss) VALUES (1,?,?)", (g, l))
        return g, l

def score_text() -> str:
    con = _connect()
    row = con.execute("SELECT green, loss FROM score WHERE id=1").fetchone()
    con.close()
    g = int((row["green"] if row else 0) or 0)
    l = int((row["loss"] if row else 0) or 0)
    total = g + l
    acc = (g/total*100.0) if total > 0 else 0.0
    return f"{g} GREEN × {l} LOSS — {acc:.1f}%"

def _get_loss_streak() -> int:
    con = _connect()
    row = con.execute("SELECT loss_streak FROM state WHERE id=1").fetchone()
    con.close()
    return int((row["loss_streak"] if row else 0) or 0)

def _set_loss_streak(v:int):
    _exec_write("UPDATE state SET loss_streak=? WHERE id=1", (int(v),))

def _bump_loss_streak(green: bool):
    _set_loss_streak(0 if green else _get_loss_streak() + 1)

def _get_last_reset_ymd() -> str:
    con = _connect()
    row = con.execute("SELECT last_reset_ymd FROM state WHERE id=1").fetchone()
    con.close()
    return (row["last_reset_ymd"] or "") if row else ""

def _set_last_reset_ymd(ymd: str):
    _exec_write("UPDATE state SET last_reset_ymd=? WHERE id=1", (ymd,))

def tz_reset_if_needed():
    today = tz_today_ymd()
    last = _get_last_reset_ymd()
    if last != today:
        _exec_write("UPDATE score SET green=0, loss=0 WHERE id=1", ())
        _set_last_reset_ymd(today)

# ========= Dedupe =========
def _is_processed(update_id: str) -> bool:
    if not update_id: return False
    con = _connect()
    row = con.execute("SELECT 1 FROM processed WHERE update_id=?", (update_id,)).fetchone()
    con.close()
    return bool(row)

def _mark_processed(update_id: str):
    if not update_id: return
    _exec_write("INSERT OR IGNORE INTO processed (update_id, seen_at) VALUES (?,?)",
                (str(update_id), now_ts()))

# ========= N-gram & Feedback =========
def timeline_size() -> int:
    con = _connect()
    row = con.execute("SELECT COUNT(*) AS c FROM timeline").fetchone()
    con.close()
    return int(row["c"] or 0)

def get_tail(limit:int=400) -> List[int]:
    con = _connect()
    rows = con.execute("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [int(r["number"]) for r in rows][::-1]

def append_seq(seq: List[int]):
    if not seq: return
    with _tx() as con:
        for n in seq:
            con.execute("INSERT INTO timeline (created_at, number) VALUES (?,?)",
                        (now_ts(), int(n)))
    _update_ngrams()

def _update_ngrams(decay: float=DECAY, max_n:int=5, window:int=400):
    tail = get_tail(window)
    if len(tail) < 2: return
    with _tx() as con:
        for t in range(1, len(tail)):
            nxt = int(tail[t])
            dist = (len(tail)-1) - t
            w = decay ** dist
            for n in range(2, max_n+1):
                if t-(n-1) < 0: break
                ctx = tail[t-(n-1):t]
                ctx_key = ",".join(str(x) for x in ctx)
                con.execute("""
                  INSERT INTO ngram (n, ctx, nxt, w)
                  VALUES (?,?,?,?)
                  ON CONFLICT(n, ctx, nxt) DO UPDATE SET w = w + excluded.w
                """, (n, ctx_key, nxt, float(w)))

def _prob_from_ngrams(ctx: List[int], cand: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    con = _connect()
    row_tot = con.execute("SELECT SUM(w) AS s FROM ngram WHERE n=? AND ctx=?", (n, ctx_key)).fetchone()
    tot = (row_tot["s"] or 0.0) if row_tot else 0.0
    if tot <= 0:
        con.close(); return 0.0
    row_c = con.execute("SELECT w FROM ngram WHERE n=? AND ctx=? AND nxt=?",
                        (n, ctx_key, int(cand))).fetchone()
    w = (row_c["w"] or 0.0) if row_c else 0.0
    con.close()
    return (w / tot) if tot > 0 else 0.0

def _ctx_to_key(ctx: List[int]) -> str:
    return ",".join(str(x) for x in ctx) if ctx else ""

def _feedback_upsert(n:int, ctx_key:str, nxt:int, delta:float):
    with _tx() as con:
        con.execute("UPDATE feedback SET w = w * ?", (FEED_DECAY,))
        con.execute("""
          INSERT INTO feedback (n, ctx, nxt, w)
          VALUES (?,?,?,?)
          ON CONFLICT(n, ctx, nxt) DO UPDATE SET w = w + excluded.w
        """, (n, ctx_key, int(nxt), float(delta)))

def _feedback_prob(n:int, ctx: List[int], cand:int) -> float:
    if not ctx: return 0.0
    ctx_key = _ctx_to_key(ctx)
    con = _connect()
    row_tot = con.execute("SELECT SUM(w) AS s FROM feedback WHERE n=? AND ctx=?", (n, ctx_key)).fetchone()
    tot = (row_tot["s"] or 0.0) if row_tot else 0.0
    if tot <= 0:
        con.close(); return 0.0
    row_c = con.execute("SELECT w FROM feedback WHERE n=? AND ctx=? AND nxt=?", (n, ctx_key, int(cand))).fetchone()
    w = (row_c["w"] or 0.0) if row_c else 0.0
    con.close()
    tot = abs(tot)
    return max(0.0, w) / (tot if tot > 0 else 1e-9)

# ========= Ensemble (4 especialistas atuais) =========
def _post_freq_k(tail: List[int], k: int) -> Dict[int,float]:
    if not tail: return {1:0.25,2:0.25,3:0.25,4:0.25}
    win = tail[-k:] if len(tail) >= k else tail
    tot = max(1, len(win))
    return _norm_dict({c: win.count(c)/tot for c in [1,2,3,4]})

def _get_expert_w():
    con = _connect()
    row = con.execute("SELECT w1,w2,w3,w4 FROM expert_w WHERE id=1").fetchone()
    con.close()
    if not row: return (1.0, 1.0, 1.0, 1.0)
    return float(row["w1"]), float(row["w2"]), float(row["w3"]), float(row["w4"])

def _set_expert_w(w1, w2, w3, w4):
    _exec_write("UPDATE expert_w SET w1=?, w2=?, w3=?, w4=? WHERE id=1",
                (float(w1), float(w2), float(w3), float(w4)))

def _hedge_blend4(p1:Dict[int,float], p2:Dict[int,float], p3:Dict[int,float], p4:Dict[int,float]):
    w1, w2, w3, w4 = _get_expert_w()
    S = (w1 + w2 + w3 + w4) or 1e-9
    w1, w2, w3, w4 = (w1/S, w2/S, w3/S, w4/S)
    blended = {c: w1*p1.get(c,0)+w2*p2.get(c,0)+w3*p3.get(c,0)+w4*p4.get(c,0) for c in [1,2,3,4]}
    s2 = sum(blended.values()) or 1e-9
    return {k: v/s2 for k,v in blended.items()}, (w1,w2,w3,w4)

def _hedge_update4(true_c:int, p1:Dict[int,float], p2:Dict[int,float], p3:Dict[int,float], p4:Dict[int,float]):
    w1, w2, w3, w4 = _get_expert_w()
    loss = lambda p: 1.0 - p.get(true_c, 0.0)
    from math import exp
    w1n = w1 * exp(-HEDGE_ETA * (1.0 - loss(p1)))
    w2n = w2 * exp(-HEDGE_ETA * (1.0 - loss(p2)))
    w3n = w3 * exp(-HEDGE_ETA * (1.0 - loss(p3)))
    w4n = w4 * exp(-HEDGE_ETA * (1.0 - loss(p4)))
    S = (w1n + w2n + w3n + w4n) or 1e-9
    _set_expert_w(w1n/S, w2n/S, w3n/S, w4n/S)

# ========= Decisão G0 =========
def _decision_context(after: Optional[int]) -> Tuple[List[int], List[int], List[int], List[int]]:
    tail = get_tail(400)
    if tail and after is not None and after in tail:
        idxs = [i for i,v in enumerate(tail) if v == after]
        i = idxs[-1]
        ctx1 = tail[max(0,i):i+1]
        ctx2 = tail[max(0,i-1):i+1] if i-1>=0 else []
        ctx3 = tail[max(0,i-2):i+1] if i-2>=0 else []
        ctx4 = tail[max(0,i-3):i+1] if i-3>=0 else []
    else:
        ctx4 = tail[-4:] if len(tail)>=4 else []
        ctx3 = tail[-3:] if len(tail)>=3 else []
        ctx2 = tail[-2:] if len(tail)>=2 else []
        ctx1 = tail[-1:] if len(tail)>=1 else []
    return ctx1, ctx2, ctx3, ctx4

def _post_from_tail(tail: List[int], after: Optional[int]) -> Dict[int, float]:
    cands = [1,2,3,4]
    scores = {c: 0.0 for c in cands}
    if not tail:
        return {c: 0.25 for c in cands}
    if after is not None and after in tail:
        idxs = [i for i,v in enumerate(tail) if v == after]
        i = idxs[-1]
        ctx1 = tail[max(0,i):i+1]
        ctx2 = tail[max(0,i-1):i+1] if i-1>=0 else []
        ctx3 = tail[max(0,i-2):i+1] if i-2>=0 else []
        ctx4 = tail[max(0,i-3):i+1] if i-3>=0 else []
    else:
        ctx4 = tail[-4:] if len(tail)>=4 else []
        ctx3 = tail[-3:] if len(tail)>=3 else []
        ctx2 = tail[-2:] if len(tail)>=2 else []
        ctx1 = tail[-1:] if len(tail)>=1 else []
    for c in cands:
        s = 0.0
        if len(ctx4)==4: s += W4 * _prob_from_ngrams(ctx4[:-1], c)
        if len(ctx3)==3: s += W3 * _prob_from_ngrams(ctx3[:-1], c)
        if len(ctx2)==2: s += W2 * _prob_from_ngrams(ctx2[:-1], c)
        if len(ctx1)==1: s += W1 * _prob_from_ngrams(ctx1[:-1], c)
        if len(ctx4)==4: s += FEED_BETA * WF4 * _feedback_prob(4, ctx4[:-1], c)
        if len(ctx3)==3: s += FEED_BETA * WF3 * _feedback_prob(3, ctx3[:-1], c)
        if len(ctx2)==2: s += FEED_BETA * WF2 * _feedback_prob(2, ctx2[:-1], c)
        if len(ctx1)==1: s += FEED_BETA * WF1 * _feedback_prob(1, ctx1[:-1], c)
        scores[c] = s
    tot = sum(scores.values()) or 1e-9
    return {k: v/tot for k,v in scores.items()}

def choose_single_number(after: Optional[int]):
    tail = get_tail(400)
    post_e1 = _post_from_tail(tail, after)         # n-gram + feedback
    post_e2 = _post_freq_k(tail, K_SHORT)          # frequência curta
    post_e3 = _post_freq_k(tail, K_LONG)           # frequência longa
    post_e4 = {1:0.25,2:0.25,3:0.25,4:0.25}        # slot p/ IA local (desligada aqui)
    post, (w1,w2,w3,w4) = _hedge_blend4(post_e1, post_e2, post_e3, post_e4)
    ranking = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    best = ranking[0][0]
    conf = float(post[best])
    r2 = ranking[:2]
    gap = (r2[0][1] - r2[1][1]) if len(r2)==2 else r2[0][1]
    return best, conf, timeline_size(), post, gap, "GEN"

# ========= Parsing =========
ENTRY_RX = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
SEQ_RX   = re.compile(r"Sequ[eê]ncia:\s*([^\n\r]+)", re.I)
AFTER_RX = re.compile(r"ap[oó]s\s+o\s+([1-4])", re.I)
GALE1_RX = re.compile(r"Estamos\s+no\s*1[ºo]\s*gale", re.I)
GALE2_RX = re.compile(r"Estamos\s+no\s*2[ºo]\s*gale", re.I)
ANALISANDO_RX = re.compile(r"\bANALISANDO\b", re.I)

GREEN_RX = re.compile(r"(?:\bgr+e+e?n\b|\bwin\b|✅)", re.I)
LOSS_RX  = re.compile(r"(?:\blo+s+s?\b|\bred\b|❌|\bperdemos\b)", re.I)

PAREN_GROUP_RX = re.compile(r"\(([^)]*)\)")
ANY_14_RX      = re.compile(r"[1-4]")

KEYCAP_MAP = {"1️⃣":"1","2️⃣":"2","3️⃣":"3","4️⃣":"4"}
def _normalize_keycaps(s: str) -> str:
    return "".join(KEYCAP_MAP.get(ch, ch) for ch in (s or ""))

def parse_entry_text(text: str) -> Optional[Dict]:
    t = _normalize_keycaps(re.sub(r"\s+", " ", text).strip())
    if not ENTRY_RX.search(t): return None
    mseq = SEQ_RX.search(t)
    seq = []
    if mseq:
        parts = re.findall(r"[1-4]", _normalize_keycaps(mseq.group(1)))
        seq = [int(x) for x in parts]
    mafter = AFTER_RX.search(t)
    after_num = int(mafter.group(1)) if mafter else None
    return {"seq": seq, "after": after_num, "raw": t}

def parse_close_numbers(text: str) -> List[int]:
    t = _normalize_keycaps(re.sub(r"\s+", " ", text))
    groups = PAREN_GROUP_RX.findall(t)
    if groups:
        last = groups[-1]
        nums = re.findall(r"[1-4]", _normalize_keycaps(last))
        return [int(x) for x in nums][:3]
    nums = ANY_14_RX.findall(t)
    return [int(x) for x in nums][:3]

# ========= Pending helpers =========
def get_open_pending() -> Optional[sqlite3.Row]:
    con = _connect()
    row = con.execute("SELECT * FROM pending WHERE open=1 ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return row

def set_stage(stage:int):
    with _tx() as con:
        con.execute("UPDATE pending SET stage=? WHERE open=1", (int(stage),))

def _seen_list(row: sqlite3.Row) -> List[str]:
    seen = (row["seen"] or "").strip()
    return [s for s in seen.split("-") if s]

def _seen_append(row: sqlite3.Row, new_items: List[str], max_len:int=1):
    cur_seen = _seen_list(row)
    for it in new_items:
        if len(cur_seen) >= max_len: break
        if it not in cur_seen:
            cur_seen.append(it)
    seen_txt = "-".join(cur_seen[:max_len])
    with _tx() as con:
        con.execute("UPDATE pending SET seen=? WHERE id=?", (seen_txt, int(row["id"])))

def _stage_from_observed(suggested: int, obs: List[int]) -> Tuple[str, str]:
    """
    MODO G0: considera apenas o primeiro observado.
    GREEN se o 1º observado == sugerido; senão LOSS. Rótulo sempre 'G0'.
    """
    if not obs:
        return ("LOSS", "G0")
    return ("GREEN", "G0") if obs[0] == suggested else ("LOSS", "G0")

def _ngram_snapshot_text(suggested: int) -> str:
    tail = get_tail(400)
    post = _post_from_tail(tail, after=None)
    def pct(x: float) -> str:
        try: return f"{x*100:.1f}%"
        except Exception: return "0.0%"
    p1 = pct(post.get(1, 0.0)); p2 = pct(post.get(2, 0.0))
    p3 = pct(post.get(3, 0.0)); p4 = pct(post.get(4, 0.0))
    conf = pct(post.get(int(suggested), 0.0))
    amostra = timeline_size()
    line1 = f"📈 Amostra: {amostra} • Conf: {conf}"
    line2 = f"🔎 E1(n-gram+fb): 1 {p1} | 2 {p2} | 3 {p3} | 4 {p4}"
    return line1 + "\n\n" + line2

def _close_with_outcome(row: sqlite3.Row, outcome: str, final_seen: str, stage_lbl: str, suggested: int):
    with _tx() as con:
        con.execute("UPDATE pending SET open=0, seen=? WHERE id=?", (final_seen, int(row["id"])))

    bump_score(outcome.upper())
    _bump_loss_streak(green=(outcome.upper()=="GREEN"))

    # feedback + timeline
    try:
        ctxs = []
        for ncol in ("ctx1","ctx2","ctx3","ctx4"):
            v = (row[ncol] or "").strip()
            ctx = [int(x) for x in v.split(",") if x.strip().isdigit()]
            ctxs.append(ctx)
        ctx1, ctx2, ctx3, ctx4 = ctxs

        if outcome.upper() == "GREEN":
            delta = FEED_POS * 1.00  # G0 peso máximo
            for (n,ctx) in [(1,ctx1),(2,ctx2),(3,ctx3),(4,ctx4)]:
                if len(ctx)>=1:
                    _feedback_upsert(n, _ctx_to_key(ctx[:-1]), suggested, delta)
        else:
            true_first = None
            try:
                true_first = next(int(x) for x in final_seen.split("-") if x.isdigit())
            except StopIteration:
                pass
            for (n,ctx) in [(1,ctx1),(2,ctx2),(3,ctx3),(4,ctx4)]:
                if len(ctx)>=1:
                    _feedback_upsert(n, _ctx_to_key(ctx[:-1]), suggested, -1.5*FEED_NEG)
                    if true_first is not None:
                        _feedback_upsert(n, _ctx_to_key(ctx[:-1]), true_first, +1.2*FEED_POS)

        # timeline: adiciona os observados (1º apenas em G0)
        obs_add = [int(x) for x in final_seen.split("-") if x.isdigit()]
        append_seq(obs_add)
    except Exception:
        pass

    # Hedge update
    try:
        true_first = None
        try:
            true_first = next(int(x) for x in final_seen.split("-") if x.isdigit())
        except StopIteration:
            pass
        if true_first is not None:
            tail_now = get_tail(400)
            post_e1 = _post_from_tail(tail_now, after=None)
            post_e2 = _post_freq_k(tail_now, K_SHORT)
            post_e3 = _post_freq_k(tail_now, K_LONG)
            post_e4 = {1:0.25,2:0.25,3:0.25,4:0.25}
            _hedge_update4(true_first, post_e1, post_e2, post_e3, post_e4)
    except Exception:
        pass

    our_num_display = suggested if outcome.upper()=="GREEN" else "X"
    snapshot = _ngram_snapshot_text(int(suggested))
    msg = (
        f"{'🟢' if outcome.upper()=='GREEN' else '🔴'} "
        f"<b>{outcome.upper()}</b> — finalizado "
        f"(<b>G0</b>, nosso={our_num_display}, observado={final_seen}).\n"
        f"📊 Geral: {score_text()}\n\n"
        f"{snapshot}"
    )
    return msg

def _maybe_close_by_timeout():
    """G0: se passou o timeout e NÃO tem nenhum observado, fecha LOSS."""
    row = get_open_pending()
    if not row: return None
    opened_at = int(row["opened_at"] or row["created_at"] or now_ts())
    if now_ts() - opened_at < OBS_TIMEOUT_SEC:
        return None
    seen_list = _seen_list(row)
    if len(seen_list) == 0:
        suggested = int(row["suggested"] or 0)
        final_seen = ""  # nenhum número
        return _close_with_outcome(row, "LOSS", final_seen, "G0", suggested)
    return None

def _open_pending_with_ctx(suggested:int, after:Optional[int], ctx1,ctx2,ctx3,ctx4) -> bool:
    with _tx() as con:
        row = con.execute("SELECT 1 FROM pending WHERE open=1 LIMIT 1").fetchone()
        if row: return False
        con.execute("""INSERT INTO pending (created_at, suggested, stage, open, seen, opened_at, after, ctx1, ctx2, ctx3, ctx4)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (now_ts(), int(suggested), 0, 1, "", now_ts(), after,
                     _ctx_to_key(ctx1), _ctx_to_key(ctx2), _ctx_to_key(ctx3), _ctx_to_key(ctx4)))
        return True

# ========= Telegram =========
async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN: return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{TELEGRAM_API}/sendMessage",
                              json={"chat_id": chat_id, "text": text, "parse_mode": parse,
                                    "disable_web_page_preview": True})
    except Exception:
        pass

# ========= App =========
app = FastAPI(title="guardiao-webhook-G0", version="5.0.0")

# ========= Helpers de abertura =========
async def _open_suggestion(after: Optional[int], origin_tag: str):
    if get_open_pending():
        return {"ok": True, "skipped": "pending_open"}

    best, conf, samples, post, gap, reason = choose_single_number(after)
    ctx1, ctx2, ctx3, ctx4 = _decision_context(after)
    if not _open_pending_with_ctx(best, after, ctx1, ctx2, ctx3, ctx4):
        return {"ok": True, "skipped": "race_lost"}

    aft_txt = f" após {after}" if after else ""
    txt = (
        f"🤖 <b>IA SUGERE</b> — <b>{best}</b>\n"
        f"🧩 <b>Padrão:</b> GEN{aft_txt} ({origin_tag})\n"
        f"📊 <b>Conf:</b> {conf*100:.2f}% | <b>Amostra≈</b>{samples} | <b>gap≈</b>{gap*100:.1f}pp\n"
        f"🧠 <b>Modo:</b> {reason}"
    )
    await tg_send_text(TARGET_CHANNEL, txt)
    return {"ok": True, "posted": True, "best": best}

# ========= Rotas =========
@app.get("/")
async def root():
    tz_reset_if_needed()
    return {"ok": True, "service": "guardiao-webhook-G0"}

@app.get("/health")
async def health():
    tz_reset_if_needed()
    pend = get_open_pending()
    return {
        "ok": True,
        "db": DB_PATH,
        "pending_open": bool(pend),
        "pending_seen": (pend["seen"] if pend else ""),
        "time": ts_str(),
        "tz": TZ_NAME
    }

@app.post(f"/webhook/{WEBHOOK_TOKEN}")
async def webhook(request: Request):
    tz_reset_if_needed()

    data = await request.json()
    upd_id = str(data.get("update_id", "")) if isinstance(data, dict) else ""
    if _is_processed(upd_id):
        return {"ok": True, "skipped": "duplicate_update"}
    _mark_processed(upd_id)

    # timeout: se houver pendência em aberto e nada observado ainda
    timeout_msg = _maybe_close_by_timeout()
    if timeout_msg:
        await tg_send_text(TARGET_CHANNEL, timeout_msg)

    msg = data.get("channel_post") or data.get("message") \
        or data.get("edited_channel_post") or data.get("edited_message") or {}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    # Filtro de origem (a menos que BYPASS_SOURCE=True)
    if SOURCE_CHANNEL and not BYPASS_SOURCE and chat_id != str(SOURCE_CHANNEL):
        if DEBUG_MSG:
            await tg_send_text(TARGET_CHANNEL, f"DEBUG: Ignorando chat {chat_id}. Fonte esperada: {SOURCE_CHANNEL}")
        return {"ok": True, "skipped": "outro_chat"}

    if not text:
        return {"ok": True, "skipped": "sem_texto"}

    # ===== MODO G0: ignorar mensagens de gale =====
    if GALE1_RX.search(text) or GALE2_RX.search(text):
        return {"ok": True, "ignored": "gale"}

    # ===== ANALISANDO: aprende sequência e pode abrir/adiantar =====
    if ANALISANDO_RX.search(_normalize_keycaps(text)):
        # aprende
        mseq = SEQ_RX.search(_normalize_keycaps(text))
        if mseq:
            seq = [int(x) for x in re.findall(r"[1-4]", mseq.group(1))]
            if seq: append_seq(seq)
        # pode abrir se não houver pendente
        if not get_open_pending():
            mafter = AFTER_RX.search(_normalize_keycaps(text))
            after = int(mafter.group(1)) if mafter else None
            r_open = await _open_suggestion(after, origin_tag="analise")
            if r_open.get("posted"):
                return r_open
        return {"ok": True, "analise": True}

    # ===== Fechamentos do fonte (GREEN/LOSS) — fecha com o 1º número =====
    if GREEN_RX.search(text) or LOSS_RX.search(text):
        pend = get_open_pending()
        if not pend:
            return {"ok": True, "noted_close_no_pending": True}

        nums = parse_close_numbers(text)  # pode vir 1..3
        if nums:
            _seen_append(pend, [str(nums[0])], max_len=1)

        pend = get_open_pending()
        seen_list = _seen_list(pend) if pend else []
        if pend and len(seen_list) >= 1:
            suggested = int(pend["suggested"] or 0)
            obs_nums = [int(x) for x in seen_list if x.isdigit()]
            outcome, stage_lbl = _stage_from_observed(suggested, obs_nums)
            final_seen = "-".join(seen_list[:1])  # só 1 observado no G0
            out_msg = _close_with_outcome(pend, outcome, final_seen, stage_lbl, suggested)
            await tg_send_text(TARGET_CHANNEL, out_msg)
            if CHAIN_ON:
                mafter = AFTER_RX.search(_normalize_keycaps(text))
                after = int(mafter.group(1)) if mafter else None
                return await _open_suggestion(after, origin_tag="after_close_G0")
            return {"ok": True, "closed_G0": outcome.lower(), "seen": final_seen}

        return {"ok": True, "noted_close_waiting_first": True}

    # ===== Nova ENTRADA CONFIRMADA (abre sugestão) =====
    parsed = parse_entry_text(text)
    if not parsed:
        if DEBUG_MSG:
            await tg_send_text(TARGET_CHANNEL, "DEBUG: Mensagem não reconhecida como ENTRADA/FECHAMENTO/ANALISANDO.")
        return {"ok": True, "skipped": "nao_eh_entrada_confirmada"}

    # Alimenta memória com a sequência (se houver)
    seq = parsed["seq"] or []
    if seq: append_seq(seq)

    after = parsed["after"]
    best, conf, samples, post, gap, reason = choose_single_number(after)

    # Abertura
    ctx1, ctx2, ctx3, ctx4 = _decision_context(after)
    opened = _open_pending_with_ctx(best, after, ctx1, ctx2, ctx3, ctx4)
    if not opened:
        if DEBUG_MSG:
            await tg_send_text(TARGET_CHANNEL, "DEBUG: pending já aberta — não abri novo.")
        return {"ok": True, "skipped": "pending_already_open"}

    aft_txt = f" após {after}" if after else ""
    txt = (
        f"🤖 <b>IA SUGERE</b> — <b>{best}</b>\n"
        f"🧩 <b>Padrão:</b> GEN{aft_txt}\n"
        f"📊 <b>Conf:</b> {conf*100:.2f}% | <b>Amostra≈</b>{samples} | <b>gap≈</b>{gap*100:.1f}pp\n"
        f"🧠 <b>Modo:</b> {reason}"
    )
    await tg_send_text(TARGET_CHANNEL, txt)
    return {"ok": True, "posted": True, "best": best, "conf": conf, "gap": gap, "samples": samples}