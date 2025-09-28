#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webhook_app.py — v4.5.0 (G0 focado + Hedge9)
- G0 puro: fecha no 1º observado (sem gales)
- Ensemble com 9 especialistas (hedge com atualização on-line)
- Anti-tilt, feedback online, reset diário, dedupe por update_id
- "ANALISANDO" aprende e pode adiantar fechamento
- Responde GREEN/LOSS em reply ao sinal correto
- MIGRAÇÃO automática da tabela expert_w (adiciona w5..w9)

ENV obrigatórias: TG_BOT_TOKEN, WEBHOOK_TOKEN
ENV opcionais:    TARGET_CHANNEL, SOURCE_CHANNEL, DB_PATH, DEBUG_MSG, BYPASS_SOURCE,
                  LLM_ENABLED, LLM_MODEL_PATH, LLM_CTX_TOKENS, LLM_N_THREADS, LLM_TEMP, LLM_TOP_P,
                  TZ_NAME
Webhook:          POST /webhook/{WEBHOOK_TOKEN}
"""

import os, re, time, sqlite3, math, json
from contextlib import contextmanager
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone

# ==== Timezone / app ====
TZ_NAME = os.getenv("TZ_NAME", "America/Sao_Paulo").strip()
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(TZ_NAME)
except Exception:
    _TZ = timezone.utc

import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= ENV =========
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "-1002796105884").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()  # vazio = não filtra
DB_PATH        = os.getenv("DB_PATH", "/var/data/data.db").strip() or "/var/data/data.db"
DEBUG_MSG      = os.getenv("DEBUG_MSG", "0").strip().lower() in ("1","true","yes")
BYPASS_SOURCE  = os.getenv("BYPASS_SOURCE", "0").strip().lower() in ("1","true","yes")
TELEGRAM_API   = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# ===== LLM (opcional) =====
LLM_ENABLED    = os.getenv("LLM_ENABLED", "1").strip().lower() in ("1","true","yes")
LLM_MODEL_PATH = os.getenv("LLM_MODEL_PATH", "models/phi-3-mini.gguf").strip()
LLM_CTX_TOKENS = int(os.getenv("LLM_CTX_TOKENS", "2048"))
LLM_N_THREADS  = int(os.getenv("LLM_N_THREADS", "4"))
LLM_TEMP       = float(os.getenv("LLM_TEMP", "0.2"))
LLM_TOP_P      = float(os.getenv("LLM_TOP_P", "0.95"))

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not WEBHOOK_TOKEN:
    raise RuntimeError("Defina WEBHOOK_TOKEN no ambiente.")

app = FastAPI(title="guardiao-auto-bot (G0 + Hedge9)", version="4.5.0")

# ========= Parâmetros =========
DECAY = 0.980
W4, W3, W2, W1 = 0.42, 0.30, 0.18, 0.10

COOLDOWN_N = 6
ALWAYS_ENTER = True

FEED_BETA   = 0.40
FEED_POS    = 0.80
FEED_NEG    = 1.30
FEED_DECAY  = 0.995
WF4, WF3, WF2, WF1 = W4, W3, W2, W1

HEDGE_ETA   = 0.6
K_SHORT     = 60
K_LONG      = 300

# ========= Utils =========
def now_ts() -> int: return int(time.time())
def ts_str(ts=None) -> str:
    if ts is None: ts = now_ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def tz_today_ymd() -> str: return datetime.now(_TZ).strftime("%Y-%m-%d")
def _softmax(d: Dict[int,float], t: float=1.0) -> Dict[int,float]:
    if not d: return {}
    if t<=0: t=1e-6
    m=max(d.values()); ex={k:math.exp((v-m)/t) for k,v in d.items()}
    s=sum(ex.values()) or 1e-9
    return {k:v/s for k,v in ex.items()}

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
        con.execute("BEGIN IMMEDIATE"); yield con; con.commit()
    except Exception:
        con.rollback(); raise
    finally:
        con.close()

def _column_exists(con: sqlite3.Connection, table: str, col: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    for r in rows:
        name = r["name"] if isinstance(r, sqlite3.Row) else r[1]
        if name == col:
            return True
    return False

def migrate_db():
    con = _connect(); cur = con.cursor()

    # timeline
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
    )""")

    # ngram
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, nxt INTEGER NOT NULL, w REAL NOT NULL,
        PRIMARY KEY (n, ctx, nxt)
    )""")

    # pending
    cur.execute("""CREATE TABLE IF NOT EXISTS pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER,
        suggested INTEGER,
        stage INTEGER DEFAULT 0,
        open INTEGER DEFAULT 1,
        seen TEXT,
        opened_at INTEGER,
        "after" INTEGER,
        ctx1 TEXT, ctx2 TEXT, ctx3 TEXT, ctx4 TEXT,
        wait_notice_sent INTEGER DEFAULT 0,
        suggest_msg_id INTEGER
    )""")

    # score
    cur.execute("""CREATE TABLE IF NOT EXISTS score (
        id INTEGER PRIMARY KEY CHECK (id=1),
        green INTEGER DEFAULT 0,
        loss  INTEGER DEFAULT 0
    )""")
    if not cur.execute("SELECT 1 FROM score WHERE id=1").fetchone():
        cur.execute("INSERT INTO score (id, green, loss) VALUES (1,0,0)")

    # feedback
    cur.execute("""CREATE TABLE IF NOT EXISTS feedback (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, nxt INTEGER NOT NULL, w REAL NOT NULL,
        PRIMARY KEY (n, ctx, nxt)
    )""")

    # processed
    cur.execute("""CREATE TABLE IF NOT EXISTS processed (
        update_id TEXT PRIMARY KEY,
        seen_at   INTEGER NOT NULL
    )""")

    # state
    cur.execute("""CREATE TABLE IF NOT EXISTS state (
        id INTEGER PRIMARY KEY CHECK (id=1),
        cooldown_left INTEGER DEFAULT 0,
        loss_streak   INTEGER DEFAULT 0,
        last_reset_ymd TEXT DEFAULT ''
    )""")
    if not cur.execute("SELECT 1 FROM state WHERE id=1").fetchone():
        cur.execute("INSERT INTO state (id, cooldown_left, loss_streak, last_reset_ymd) VALUES (1,0,0,'')")

    # expert_w base
    cur.execute("""CREATE TABLE IF NOT EXISTS expert_w (
        id INTEGER PRIMARY KEY CHECK (id=1),
        w1 REAL NOT NULL, w2 REAL NOT NULL, w3 REAL NOT NULL, w4 REAL NOT NULL
    )""")
    if not cur.execute("SELECT 1 FROM expert_w WHERE id=1").fetchone():
        cur.execute("INSERT INTO expert_w (id,w1,w2,w3,w4) VALUES (1,1,1,1,1)")

    # MIGRA: garante w5..w9
    for col in ("w5","w6","w7","w8","w9"):
        if not _column_exists(con, "expert_w", col):
            cur.execute(f"ALTER TABLE expert_w ADD COLUMN {col} REAL NOT NULL DEFAULT 1.0")

    con.commit(); con.close()
migrate_db()

def _exec_write(sql: str, params: tuple=()):
    for attempt in range(6):
        try:
            with _tx() as con: con.execute(sql, params); return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(0.25*(attempt+1)); continue
            raise

# ========= Dedupe / score / state =========
def _is_processed(update_id: str) -> bool:
    if not update_id: return False
    con = _connect(); row = con.execute("SELECT 1 FROM processed WHERE update_id=?", (update_id,)).fetchone(); con.close()
    return bool(row)
def _mark_processed(update_id: str):
    if not update_id: return
    _exec_write("INSERT OR IGNORE INTO processed (update_id, seen_at) VALUES (?,?)",(str(update_id), now_ts()))

def bump_score(outcome: str) -> Tuple[int, int]:
    with _tx() as con:
        row = con.execute("SELECT green, loss FROM score WHERE id=1").fetchone()
        g, l = (row["green"], row["loss"]) if row else (0, 0)
        if outcome.upper() == "GREEN": g += 1
        elif outcome.upper() == "LOSS": l += 1
        con.execute("INSERT OR REPLACE INTO score (id, green, loss) VALUES (1,?,?)", (g, l))
        return g, l
def reset_score(): _exec_write("INSERT OR REPLACE INTO score (id, green, loss) VALUES (1,0,0)")
def score_text() -> str:
    con = _connect(); row = con.execute("SELECT green, loss FROM score WHERE id=1").fetchone(); con.close()
    if not row: return "0 GREEN × 0 LOSS — 0.0%"
    g, l = int(row["green"]), int(row["loss"]); tot = g+l
    acc = (g/tot*100.0) if tot>0 else 0.0
    return f"{g} GREEN × {l} LOSS — {acc:.1f}%"

def _get_last_reset_ymd() -> str:
    con = _connect(); row = con.execute("SELECT last_reset_ymd FROM state WHERE id=1").fetchone(); con.close()
    return (row["last_reset_ymd"] or "") if row else ""
def _set_last_reset_ymd(ymd: str): _exec_write("UPDATE state SET last_reset_ymd=? WHERE id=1", (ymd,))
def check_and_maybe_reset_score():
    today = tz_today_ymd()
    if _get_last_reset_ymd() != today:
        reset_score(); _set_last_reset_ymd(today)

def _get_cooldown() -> int:
    con = _connect(); row = con.execute("SELECT cooldown_left FROM state WHERE id=1").fetchone(); con.close()
    return int((row["cooldown_left"] if row else 0) or 0)
def _set_cooldown(v:int): _exec_write("UPDATE state SET cooldown_left=? WHERE id=1", (int(v),))
def _dec_cooldown():
    with _tx() as con:
        row = con.execute("SELECT cooldown_left FROM state WHERE id=1").fetchone()
        cur = int((row["cooldown_left"] if row else 0) or 0); cur = max(0, cur-1)
        con.execute("UPDATE state SET cooldown_left=? WHERE id=1", (cur,))
def _get_loss_streak() -> int:
    con = _connect(); row = con.execute("SELECT loss_streak FROM state WHERE id=1").fetchone(); con.close()
    return int((row["loss_streak"] if row else 0) or 0)
def _set_loss_streak(v:int): _exec_write("UPDATE state SET loss_streak=? WHERE id=1", (int(v),))
def _bump_loss_streak(reset: bool):
    if reset: _set_loss_streak(0)
    else: _set_loss_streak(_get_loss_streak()+1)

# ========= N-gram & Feedback =========
def timeline_size() -> int:
    con = _connect(); row = con.execute("SELECT COUNT(*) AS c FROM timeline").fetchone(); con.close()
    return int(row["c"] or 0)
def get_tail(limit:int=400) -> List[int]:
    con = _connect(); rows = con.execute("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (limit,)).fetchall(); con.close()
    return [int(r["number"]) for r in rows][::-1]
def append_seq(seq: List[int]):
    if not seq: return
    with _tx() as con:
        for n in seq: con.execute("INSERT INTO timeline (created_at, number) VALUES (?,?)",(now_ts(), int(n)))
    _update_ngrams()
def _update_ngrams(decay: float=DECAY, max_n:int=5, window:int=400):
    tail = get_tail(window)
    if len(tail) < 2: return
    with _tx() as con:
        for t in range(1, len(tail)):
            nxt = int(tail[t]); dist = (len(tail)-1) - t; w = decay ** dist
            for n in range(2, max_n+1):
                if t-(n-1) < 0: break
                ctx = tail[t-(n-1):t]; ctx_key = ",".join(str(x) for x in ctx)
                con.execute("""INSERT INTO ngram (n, ctx, nxt, w) VALUES (?,?,?,?)
                               ON CONFLICT(n, ctx, nxt) DO UPDATE SET w = w + excluded.w""",
                            (n, ctx_key, nxt, float(w)))
def _prob_from_ngrams(ctx: List[int], cand: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(str(x) for x in ctx); con = _connect()
    row_tot = con.execute("SELECT SUM(w) AS s FROM ngram WHERE n=? AND ctx=?", (n, ctx_key)).fetchone()
    tot = (row_tot["s"] or 0.0) if row_tot else 0.0
    if (tot or 0) <= 0: con.close(); return 0.0
    row_c = con.execute("SELECT w FROM ngram WHERE n=? AND ctx=? AND nxt=?", (n, ctx_key, int(cand))).fetchone()
    w = (row_c["w"] or 0.0) if row_c else 0.0; con.close()
    return w / tot
def _ctx_to_key(ctx: List[int]) -> str: return ",".join(str(x) for x in ctx) if ctx else ""
def _feedback_upsert(n:int, ctx_key:str, nxt:int, delta:float):
    with _tx() as con:
        con.execute("UPDATE feedback SET w = w * ?", (FEED_DECAY,))
        con.execute("""INSERT INTO feedback (n, ctx, nxt, w) VALUES (?,?,?,?)
                       ON CONFLICT(n, ctx, nxt) DO UPDATE SET w = w + excluded.w""",
                    (n, ctx_key, int(nxt), float(delta)))
def _feedback_prob(n:int, ctx: List[int], cand:int) -> float:
    if not ctx: return 0.0
    ctx_key = _ctx_to_key(ctx); con = _connect()
    row_tot = con.execute("SELECT SUM(w) AS s FROM feedback WHERE n=? AND ctx=?", (n, ctx_key)).fetchone()
    tot = (row_tot["s"] or 0.0) if row_tot else 0.0
    if (tot or 0) <= 0: con.close(); return 0.0
    row_c = con.execute("SELECT w FROM feedback WHERE n=? AND ctx=? AND nxt=?", (n, ctx_key, int(cand))).fetchone()
    w = (row_c["w"] or 0.0) if row_c else 0.0; con.close()
    tot = abs(tot); return max(0.0, w) / (tot if tot > 0 else 1e-9)

def _decision_context(after: Optional[int]) -> Tuple[List[int], List[int], List[int], List[int]]:
    tail = get_tail(400)
    if tail and after is not None and after in tail:
        idxs = [i for i,v in enumerate(tail) if v == after]; i = idxs[-1]
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

# ========= LLM opcional =========
try:
    from llama_cpp import Llama
    _LLM = None
    def _llm_load():
        global _LLM
        if _LLM is None and LLM_ENABLED and os.path.exists(LLM_MODEL_PATH):
            _LLM = Llama(model_path=LLM_MODEL_PATH, n_ctx=LLM_CTX_TOKENS, n_threads=LLM_N_THREADS, verbose=False)
        return _LLM
except Exception:
    _LLM = None
    def _llm_load(): return None

_LLM_SYSTEM = (
    "Você prevê o próximo número de {1,2,3,4}. "
    "Responda APENAS JSON: {\"1\":p1,\"2\":p2,\"3\":p3,\"4\":p4} com probabilidades normalizadas."
)
def _llm_probs_from_tail(tail: List[int]) -> Dict[int,float]:
    llm = _llm_load()
    if llm is None or not LLM_ENABLED: return {}
    win60  = tail[-60:] if len(tail) >= 60 else tail
    win300 = tail[-300:] if len(tail) >= 300 else tail
    def freq(win, n): return win.count(n)/max(1, len(win))
    feats = {
        "len": len(tail),
        "last10": tail[-10:],
        "freq60": {n: freq(win60, n) for n in (1,2,3,4)},
        "freq300": {n: freq(win300, n) for n in (1,2,3,4)},
    }
    user = f"Historico_Recente={tail[-50:]}\nFeats={feats}\nRetorne JSON de probabilidades (%) para o próximo número."
    try:
        out = llm.create_chat_completion(
            messages=[{"role":"system","content":_LLM_SYSTEM},{"role":"user","content":user}],
            temperature=LLM_TEMP, top_p=LLM_TOP_P, max_tokens=128
        )
        text = out["choices"][0]["message"]["content"].strip()
        m = re.search(r"\{.*\}", text, re.S)
        jtxt = m.group(0) if m else text
        data = json.loads(jtxt)
        raw = {int(k): float(v) for k,v in data.items() if str(k) in ("1","2","3","4")}
        S = sum(raw.values()) or 1e-9
        return {k: max(0.0, v/S) for k,v in raw.items() if k in (1,2,3,4)}
    except Exception:
        return {}

# ========= Especialistas =========
def _post_from_tail(tail: List[int], after: Optional[int]) -> Dict[int, float]:
    cands = [1,2,3,4]; scores = {c: 0.0 for c in cands}
    if not tail: return {c: 0.25 for c in cands}
    if after is not None and after in tail:
        idxs = [i for i,v in enumerate(tail) if v == after]; i = idxs[-1]
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

def _post_freq_k(tail: List[int], k: int) -> Dict[int,float]:
    if not tail: return {1:0.25,2:0.25,3:0.25,4:0.25}
    win = tail[-k:] if len(tail) >= k else tail
    tot = max(1, len(win))
    return {c: win.count(c)/tot for c in [1,2,3,4]}

def _post_entropy_balancer(tail: List[int]) -> Dict[int,float]:
    f60 = _post_freq_k(tail, 60)
    H = -sum((p+1e-12)*math.log(p+1e-12,4) for p in f60.values())
    mix = 0.35 if H < 0.6 else 0.15
    uni = {c:0.25 for c in [1,2,3,4]}
    return {c:(1-mix)*f60.get(c,0.0)+mix*uni[c] for c in [1,2,3,4]}

def _post_markov_1(tail: List[int]) -> Dict[int,float]:
    if len(tail) < 2: return {c:0.25 for c in [1,2,3,4]}
    counts = {c:{d:1.0 for d in [1,2,3,4]} for c in [1,2,3,4]}
    for i in range(1, len(tail)): counts[tail[i-1]][tail[i]] += 1.0
    row = counts[tail[-1]]; S = sum(row.values()) or 1e-9
    return {d: row[d]/S for d in [1,2,3,4]}

def _post_exp_decay_freq(tail: List[int]) -> Dict[int,float]:
    if not tail: return {c:0.25 for c in [1,2,3,4]}
    lam = 0.95; score = {c:0.0 for c in [1,2,3,4]}; w=1.0
    for v in reversed(tail): score[v]+=w; w*=lam
    s = sum(score.values()) or 1e-9
    return {k:v/s for k,v in score.items()}

def _post_pair_table(tail: List[int]) -> Dict[int,float]:
    if len(tail) < 2: return {c:0.25 for c in [1,2,3,4]}
    pair = {}
    for i in range(1, len(tail)-1):
        a,b = tail[i-1], tail[i]
        pair.setdefault((a,b), {c:1.0 for c in [1,2,3,4]})
        pair[(a,b)][tail[i+1]] = pair[(a,b)].get(tail[i+1],1.0) + 1.0
    a,b = tail[-2], tail[-1]
    dist = pair.get((a,b), {c:1.0 for c in [1,2,3,4]})
    s = sum(dist.values()) or 1e-9
    return {k:v/s for k,v in dist.items()}

def _post_recency_penalty(tail: List[int]) -> Dict[int,float]:
    if not tail: return {c:0.25 for c in [1,2,3,4]}
    last_pos = {c:-9999 for c in [1,2,3,4]}
    for idx,v in enumerate(tail): last_pos[v]=idx
    n=len(tail)-1
    gaps={c:(n-last_pos[c]) for c in [1,2,3,4]}
    return _softmax(gaps, t=3.0)

# ========= Hedge 9 =========
def _get_expert_w() -> List[float]:
    con = _connect()
    # garante colunas (instâncias paralelas)
    for col in ("w5","w6","w7","w8","w9"):
        if not _column_exists(con, "expert_w", col):
            con.execute(f"ALTER TABLE expert_w ADD COLUMN {col} REAL NOT NULL DEFAULT 1.0")
    row = con.execute("SELECT w1,w2,w3,w4,w5,w6,w7,w8,w9 FROM expert_w WHERE id=1").fetchone()
    con.close()
    if not row: return [1.0]*9
    return [float(row[f"w{i}"]) for i in range(1,10)]

def _set_expert_w(ws: List[float]):
    with _tx() as con:
        con.execute("""UPDATE expert_w SET
                       w1=?,w2=?,w3=?,w4=?,w5=?,w6=?,w7=?,w8=?,w9=? WHERE id=1""",
                    tuple(float(x) for x in ws))

def _blend9(posts: List[Dict[int,float]]) -> Tuple[Dict[int,float], List[float]]:
    ws = _get_expert_w(); S = sum(ws) or 1e-9; ws = [w/S for w in ws]
    agg = {c:0.0 for c in [1,2,3,4]}
    for w,dist in zip(ws, posts):
        for c in [1,2,3,4]:
            agg[c] += w * float(dist.get(c,0.0))
    s2 = sum(agg.values()) or 1e-9
    return {k:v/s2 for k,v in agg.items()}, ws

def _hedge_update9(true_c:int, posts: List[Dict[int,float]]):
    ws = _get_expert_w()
    from math import exp
    new = []
    for w,dist in zip(ws, posts):
        loss = 1.0 - float(dist.get(true_c,0.0))
        new.append(w * exp(-HEDGE_ETA * (1.0 - loss)))
    S = sum(new) or 1e-9
    _set_expert_w([x/S for x in new])

# ========= Anti-tilt =========
def _streak_adjust_choice(post:Dict[int,float], gap:float, ls:int) -> Tuple[int,str,Dict[int,float]]:
    reason = "Hedge9"
    ranking = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    best = ranking[0][0]
    if ls >= 3:
        comp = {c: max(1e-9, 1.0 - post[c]) for c in [1,2,3,4]}
        s = sum(comp.values()) or 1e-9; comp = {k:v/s for k,v in comp.items()}
        post = {c: 0.7*post[c] + 0.3*comp[c] for c in [1,2,3,4]}
        S = sum(post.values()) or 1e-9; post = {k:v/S for k,v in post.items()}
        ranking = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
        best = ranking[0][0]; reason = "Hedge9_anti_tilt"
    if ls >= 2:
        top2 = ranking[:2]
        if len(top2) == 2 and gap < 0.05:
            best = top2[1][0]; reason = "Hedge9_runnerup_ls2"
    return best, reason, post

def choose_single_number(after: Optional[int]):
    tail = get_tail(400)
    e1 = _post_from_tail(tail, after)
    e2 = _post_freq_k(tail, K_SHORT)
    e3 = _post_freq_k(tail, K_LONG)
    e4 = (_llm_probs_from_tail(tail) or {1:0.25,2:0.25,3:0.25,4:0.25}) if LLM_ENABLED else {1:0.25,2:0.25,3:0.25,4:0.25}
    e5 = _post_entropy_balancer(tail)
    e6 = _post_markov_1(tail)
    e7 = _post_exp_decay_freq(tail)
    e8 = _post_pair_table(tail)
    e9 = _post_recency_penalty(tail)
    post, ws = _blend9([e1,e2,e3,e4,e5,e6,e7,e8,e9])
    ranking = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    top2 = ranking[:2]
    gap = (top2[0][1] - top2[1][1]) if len(top2) >= 2 else ranking[0][1]
    base_best = ranking[0][0]
    conf = float(post[base_best])
    ls = _get_loss_streak()
    best, reason, post_adj = _streak_adjust_choice(post, gap, ls)
    conf = float(post_adj[best])
    r2 = sorted(post_adj.items(), key=lambda kv: kv[1], reverse=True)[:2]
    gap2 = (r2[0][1] - r2[1][1]) if len(r2) == 2 else r2[0][1]
    return best, conf, timeline_size(), post_adj, gap2, reason

# ========= Parse =========
ENTRY_RX = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
SEQ_RX   = re.compile(r"Sequ[eê]ncia:\s*([^\n\r]+)", re.I)
AFTER_RX = re.compile(r"ap[oó]s\s+o\s+([1-4])", re.I)
ANALISANDO_RX = re.compile(r"\bANALISANDO\b", re.I)
GREEN_RX = re.compile(r"(?:\bgr+e+e?n\b|\bwin\b|✅)", re.I)
LOSS_RX  = re.compile(r"(?:\blo+s+s?\b|\bred\b|❌|\bperdemos\b)", re.I)
PAREN_GROUP_RX = re.compile(r"\(([^)]*)\)")
ANY_14_RX      = re.compile(r"[1-4]")
KEYCAP_MAP = {"1️⃣":"1","2️⃣":"2","3️⃣":"3","4️⃣":"4"}
def _normalize_keycaps(s: str) -> str: return "".join(KEYCAP_MAP.get(ch, ch) for ch in (s or ""))

def parse_entry_text(text: str) -> Optional[Dict]:
    t = _normalize_keycaps(re.sub(r"\s+", " ", text).strip())
    if not ENTRY_RX.search(t): return None
    mseq = SEQ_RX.search(t); seq=[]
    if mseq: seq = [int(x) for x in re.findall(r"[1-4]", _normalize_keycaps(mseq.group(1)))]
    mafter = AFTER_RX.search(t); after_num = int(mafter.group(1)) if mafter else None
    return {"seq": seq, "after": after_num, "raw": t}

def parse_close_numbers(text: str) -> List[int]:
    t = _normalize_keycaps(re.sub(r"\s+", " ", text))
    groups = PAREN_GROUP_RX.findall(t)
    if groups:
        last = groups[-1]
        nums = re.findall(r"[1-4]", _normalize_keycaps(last))
        return [int(x) for x in nums][:1]  # G0: só o primeiro observado
    nums = ANY_14_RX.findall(t)
    return [int(x) for x in nums][:1]

# ========= Pending (G0) =========
def get_open_pending() -> Optional[sqlite3.Row]:
    con = _connect(); row = con.execute("SELECT * FROM pending WHERE open=1 ORDER BY id DESC LIMIT 1").fetchone(); con.close()
    return row
def set_stage(stage:int):
    with _tx() as con: con.execute("UPDATE pending SET stage=? WHERE open=1", (int(stage),))
def _seen_list(row: sqlite3.Row) -> List[str]:
    seen = (row["seen"] or "").strip()
    return [s for s in seen.split("-") if s]
def _seen_append(row: sqlite3.Row, new_items: List[str]):
    cur_seen = _seen_list(row)
    for it in new_items:
        if len(cur_seen) >= 1: break  # G0: 1 observado só
        if it not in cur_seen: cur_seen.append(it)
    seen_txt = "-".join(cur_seen[:1])
    with _tx() as con: con.execute("UPDATE pending SET seen=? WHERE id=?", (seen_txt, int(row["id"])))
def _stage_from_observed_g0(suggested: int, obs_first: Optional[int]) -> Tuple[str, str]:
    if obs_first is None: return ("LOSS", "G0")
    return ("GREEN","G0") if int(obs_first)==int(suggested) else ("LOSS","G0")

def _ngram_snapshot_text(suggested: int) -> str:
    tail = get_tail(400); post = _post_from_tail(tail, after=None)
    pct = lambda x: f"{x*100:.1f}%"
    p1,p2,p3,p4 = pct(post.get(1,0.0)), pct(post.get(2,0.0)), pct(post.get(3,0.0)), pct(post.get(4,0.0))
    conf = pct(post.get(int(suggested),0.0)); amostra = timeline_size()
    return f"📈 Amostra: {amostra} • Conf: {conf}\n\n🔎 E1(n-gram+fb): 1 {p1} | 2 {p2} | 3 {p3} | 4 {p4}"

def _open_pending_with_ctx(suggested:int, after:Optional[int], ctx1,ctx2,ctx3,ctx4) -> bool:
    with _tx() as con:
        row = con.execute("SELECT 1 FROM pending WHERE open=1 LIMIT 1").fetchone()
        if row: return False
        con.execute("""INSERT INTO pending (created_at, suggested, stage, open, seen, opened_at, after, ctx1, ctx2, ctx3, ctx4, wait_notice_sent, suggest_msg_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,0,NULL)""",
                    (now_ts(), int(suggested), 0, 1, "", now_ts(), after,
                     _ctx_to_key(ctx1), _ctx_to_key(ctx2), _ctx_to_key(ctx3), _ctx_to_key(ctx4)))
        return True

def _save_suggest_msg_id(msg_id: Optional[int]):
    if msg_id is None: return
    with _tx() as con:
        con.execute("UPDATE pending SET suggest_msg_id=? WHERE open=1 ORDER BY id DESC LIMIT 1", (int(msg_id),))

def _close_now(row: sqlite3.Row) -> Dict:
    seen_list = _seen_list(row)
    obs_first = int(seen_list[0]) if seen_list and seen_list[0].isdigit() else None
    suggested = int(row["suggested"] or 0)
    outcome, stage_lbl = _stage_from_observed_g0(suggested, obs_first)

    with _tx() as con: con.execute("UPDATE pending SET open=0 WHERE id=?", (int(row["id"]),))

    # feedback leve + hedge update
    try:
        tail = get_tail(5)
        if obs_first is not None:
            _feedback_upsert(2, _ctx_to_key(tail[-1:]), obs_first, +FEED_POS)
    except Exception: pass
    try:
        tail_now = get_tail(400)
        posts = [
            _post_from_tail(tail_now, after=None),
            _post_freq_k(tail_now, K_SHORT),
            _post_freq_k(tail_now, K_LONG),
            (_llm_probs_from_tail(tail_now) or {1:0.25,2:0.25,3:0.25,4:0.25}) if LLM_ENABLED else {1:0.25,2:0.25,3:0.25,4:0.25},
            _post_entropy_balancer(tail_now),
            _post_markov_1(tail_now),
            _post_exp_decay_freq(tail_now),
            _post_pair_table(tail_now),
            _post_recency_penalty(tail_now),
        ]
        if obs_first is not None: _hedge_update9(obs_first, posts)
    except Exception: pass

    bump_score(outcome.upper())
    try:
        if outcome.upper()=="LOSS": _set_cooldown(COOLDOWN_N); _bump_loss_streak(False)
        else: _dec_cooldown(); _bump_loss_streak(True)
    except Exception: pass

    snapshot = _ngram_snapshot_text(int(suggested))
    our_num_display = suggested if outcome.upper()=="GREEN" else "X"
    msg = (f"{'🟢' if outcome.upper()=='GREEN' else '🔴'} <b>{outcome.upper()}</b> — finalizado "
           f"(G0, nosso={our_num_display}, observados={obs_first}).\n"
           f"📊 Geral: {score_text()}\n\n{snapshot}")

    reply_to=None
    try:
        con=_connect(); r2=con.execute("SELECT suggest_msg_id FROM pending WHERE id=?", (int(row["id"]),)).fetchone(); con.close()
        reply_to = int(r2["suggest_msg_id"] or 0) if r2 and r2["suggest_msg_id"] else None
    except Exception: pass
    return {"closed": True, "msg": msg, "reply_to": reply_to}

# ========= Telegram =========
async def tg_send_text(chat_id: str, text: str, parse: str="HTML", reply_to: int=None):
    if not TG_BOT_TOKEN: return None
    payload={"chat_id": chat_id,"text": text,"parse_mode": parse,"disable_web_page_preview": True}
    if reply_to is not None:
        payload["reply_to_message_id"]=int(reply_to)
        payload["allow_sending_without_reply"]=True
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r=await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
            data=r.json(); return (data.get("result") or {}).get("message_id")
    except Exception:
        return None

# ========= Rotas =========
@app.get("/")
async def root():
    check_and_maybe_reset_score()
    return {"ok": True, "service": "guardiao-auto-bot (G0 + Hedge9)"}

@app.get("/health")
async def health():
    check_and_maybe_reset_score()
    pend = get_open_pending()
    return {"ok": True, "db": DB_PATH, "pending_open": bool(pend), "pending_seen": (pend["seen"] if pend else ""), "time": ts_str(), "tz": TZ_NAME}

ENTRY_RX = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)  # definido antes mas ok

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    check_and_maybe_reset_score()
    data = await request.json()

    # Dedupe
    upd_id = str(data.get("update_id", "")) if isinstance(data, dict) else ""
    if _is_processed(upd_id): return {"ok": True, "skipped": "duplicate_update"}
    _mark_processed(upd_id)

    msg = data.get("channel_post") or data.get("message") or data.get("edited_channel_post") or data.get("edited_message") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    chat = msg.get("chat") or {}; chat_id = str(chat.get("id") or "")

    if SOURCE_CHANNEL and not BYPASS_SOURCE and chat_id != str(SOURCE_CHANNEL):
        if DEBUG_MSG: await tg_send_text(TARGET_CHANNEL, f"DEBUG: Ignorando chat {chat_id}. Fonte esperada: {SOURCE_CHANNEL}")
        return {"ok": True, "skipped": "outro_chat"}
    if not text: return {"ok": True, "skipped": "sem_texto"}

    # ANALISANDO: aprende e pode fechar G0
    if ANALISANDO_RX.search(_normalize_keycaps(text)):
        mseq = SEQ_RX.search(_normalize_keycaps(text))
        seq = [int(x) for x in re.findall(r"[1-4]", mseq.group(1))] if mseq else []
        if seq: append_seq(seq)
        pend = get_open_pending()
        if pend and seq:
            _seen_append(pend, [str(seq[0])])
            pend = get_open_pending()
            if pend and (pend["seen"] or "").strip():
                result = _close_now(pend)
                await tg_send_text(TARGET_CHANNEL, result["msg"], reply_to=result.get("reply_to"))
                return {"ok": True, "closed_from_analise": True}
        return {"ok": True, "analise_seen": len(seq)}

    # Fechamentos do fonte (GREEN/LOSS) — no G0 basta 1 observado
    if GREEN_RX.search(text) or LOSS_RX.search(text):
        pend = get_open_pending()
        if pend:
            nums = parse_close_numbers(text)
            if nums: _seen_append(pend, [str(n) for n in nums])
            pend = get_open_pending()
            if pend and (pend["seen"] or "").strip():
                result = _close_now(pend)
                await tg_send_text(TARGET_CHANNEL, result["msg"], reply_to=result.get("reply_to"))
                return {"ok": True, "closed": True}
        return {"ok": True, "noted_close": True}

    # Nova ENTRADA CONFIRMADA (abre 1 pending)
    parsed = parse_entry_text(text)
    if not parsed:
        if DEBUG_MSG: await tg_send_text(TARGET_CHANNEL, "DEBUG: Mensagem não reconhecida como ENTRADA/FECHAMENTO/ANALISANDO.")
        return {"ok": True, "skipped": "nao_eh_entrada_confirmada"}

    if get_open_pending():
        return {"ok": True, "kept_open_waiting_close": True}

    # memória + decisão + abertura
    seq = parsed["seq"] or []
    if seq: append_seq(seq)
    after = parsed["after"]
    best, conf, samples, post, gap, reason = choose_single_number(after)
    ctx1, ctx2, ctx3, ctx4 = _decision_context(after)

    if _open_pending_with_ctx(best, after, ctx1, ctx2, ctx3, ctx4):
        aft_txt = f" após {after}" if after else ""
        ls = _get_loss_streak()
        txt = (
            f"🤖 <b>IA SUGERE</b> — <b>{best}</b>\n"
            f"🧩 <b>Padrão:</b> GEN{aft_txt}\n"
            f"📊 <b>Conf:</b> {conf*100:.2f}% | <b>Amostra≈</b>{samples} | <b>gap≈</b>{gap*100:.1f}pp\n"
            f"🧠 <b>Modo:</b> {reason} | <b>streak RED:</b> {ls}"
        )
        msg_id = await tg_send_text(TARGET_CHANNEL, txt)
        _save_suggest_msg_id(msg_id)
        return {"ok": True, "posted": True, "best": best, "conf": conf, "gap": gap, "samples": samples}

    if DEBUG_MSG: await tg_send_text(TARGET_CHANNEL, "DEBUG: Já existe pending open — não abri novo.")
    return {"ok": True, "skipped": "pending_already_open"}