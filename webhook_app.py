#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webhook_app.py — G0 only — 10 especialistas (E1..E10)
- Fluxo G0 puro: decisão única (sem gales)
- Aprende com "ANALISANDO" (Sequência)
- Dedupe por update_id (tabela processed)
- Filtro de origem (SOURCE_CHANNEL) opcional
- Abre sugestão, fecha com 1º observado (G0) e publica 🟢/🔴 em reply à sugestão
- (Opcional) abre novo sinal imediatamente após fechar (OPEN_AFTER_CLOSE=on)
- DB SQLite com WAL + busy_timeout
- Configuração via ENV:
  TG_BOT_TOKEN, WEBHOOK_TOKEN, TARGET_CHANNEL, SOURCE_CHANNEL, DB_PATH, DEBUG_MSG, BYPASS_SOURCE, TZ_NAME
  CONF_MIN, GAP_MIN, ENSEMBLE_TEMP, META_TEMP, HISTORY_WINDOW, NGRAM_DECAY, FEEDBACK_DECAY, FEEDBACK_REINFORCE, OPEN_AFTER_CLOSE
"""

import os, re, time, sqlite3, math, json
from contextlib import contextmanager
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone

# ========= ENV =========
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "-1002796105884").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()  # vazio = sem filtro
DB_PATH        = os.getenv("DB_PATH", "/var/data/data.db").strip() or "/var/data/data.db"
DEBUG_MSG      = os.getenv("DEBUG_MSG", "0").strip().lower() in ("1","true","yes")
BYPASS_SOURCE  = os.getenv("BYPASS_SOURCE", "0").strip().lower() in ("1","true","yes")
TZ_NAME        = os.getenv("TZ_NAME", "America/Sao_Paulo").strip()

# Ajustes (todos opcionais) — informativos/algorítmicos
CONF_MIN        = float(os.getenv("CONF_MIN", "0.00"))
GAP_MIN         = float(os.getenv("GAP_MIN", "0.00"))
ENSEMBLE_TEMP   = float(os.getenv("ENSEMBLE_TEMP", "0.75"))
META_TEMP       = float(os.getenv("META_TEMP", "0.60"))
HISTORY_WINDOW  = int(os.getenv("HISTORY_WINDOW", "400"))
NGRAM_DECAY     = float(os.getenv("NGRAM_DECAY", "0.980"))
FEEDBACK_DECAY  = float(os.getenv("FEEDBACK_DECAY", "0.995"))
FEEDBACK_REINFORCE = float(os.getenv("FEEDBACK_REINFORCE", "0.80"))
OPEN_AFTER_CLOSE   = os.getenv("OPEN_AFTER_CLOSE", "on").strip().lower() in ("1","true","yes","on")

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not WEBHOOK_TOKEN:
    raise RuntimeError("Defina WEBHOOK_TOKEN no ambiente.")

# ========= Web =========
import httpx
from fastapi import FastAPI, Request, HTTPException
app = FastAPI(title="guardiao-g0-10x", version="1.1.0")

# ========= Timezone =========
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(TZ_NAME)
except Exception:
    _TZ = timezone.utc

# ========= Utils =========
def now_ts() -> int:
    return int(time.time())

def ts_str(ts: Optional[int] = None) -> str:
    if ts is None: ts = now_ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _norm(d: Dict[int, float]) -> Dict[int, float]:
    s = float(sum(d.values())) or 1e-9
    return {k: max(0.0, v)/s for k, v in d.items()}

def _softmax(d: Dict[int, float], t: float = 1.0) -> Dict[int, float]:
    if t <= 0: t = 1e-6
    m = max(d.values()) if d else 0.0
    ex = {k: math.exp((v - m) / t) for k, v in d.items()}
    s = sum(ex.values()) or 1e-9
    return {k: v/s for k, v in ex.items()}

def _gap_top2(dist: Dict[int,float]) -> float:
    r = sorted(dist.values(), reverse=True)
    if not r: return 0.0
    if len(r) == 1: return r[0]
    return max(0.0, r[0]-r[1])

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

def _ensure_tables():
    con = _connect(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS processed(
        update_id TEXT PRIMARY KEY,
        seen_at   INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram(
        n INTEGER NOT NULL, ctx TEXT NOT NULL, nxt INTEGER NOT NULL, w REAL NOT NULL,
        PRIMARY KEY (n, ctx, nxt)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS feedback(
        n INTEGER NOT NULL, ctx TEXT NOT NULL, nxt INTEGER NOT NULL, w REAL NOT NULL,
        PRIMARY KEY (n, ctx, nxt)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS score(
        id INTEGER PRIMARY KEY CHECK(id=1),
        green INTEGER DEFAULT 0,
        loss  INTEGER DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER,
        suggested INTEGER,
        open INTEGER DEFAULT 1,
        seen TEXT,
        opened_at INTEGER,
        suggest_msg_id INTEGER
    )""")
    # migração leve (ignora erro se já existir)
    try:
        cur.execute("ALTER TABLE pending ADD COLUMN suggest_msg_id INTEGER")
    except sqlite3.OperationalError:
        pass
    if not cur.execute("SELECT 1 FROM score WHERE id=1").fetchone():
        cur.execute("INSERT INTO score(id,green,loss) VALUES(1,0,0)")
    con.commit(); con.close()
_ensure_tables()

# ========= Score helpers =========
def bump_score(outcome: str) -> Tuple[int,int]:
    with _tx() as con:
        row = con.execute("SELECT green,loss FROM score WHERE id=1").fetchone()
        g,l = (row["green"], row["loss"]) if row else (0,0)
        if outcome.upper()=="GREEN": g+=1
        elif outcome.upper()=="LOSS": l+=1
        con.execute("INSERT OR REPLACE INTO score(id,green,loss) VALUES(1,?,?)",(g,l))
        return g,l

def score_text() -> str:
    con = _connect()
    row = con.execute("SELECT green,loss FROM score WHERE id=1").fetchone()
    con.close()
    g,l = (row["green"], row["loss"]) if row else (0,0)
    tot = g+l
    acc = (g/tot*100.0) if tot>0 else 0.0
    return f"{g} GREEN × {l} LOSS — {acc:.1f}%"

# ========= Dedupe =========
def _is_processed(upd_id: str) -> bool:
    if not upd_id:
        return False
    con = _connect()
    try:
        row = con.execute("SELECT 1 FROM processed WHERE update_id=?",
                          (str(upd_id),)).fetchone()
        return bool(row)
    finally:
        con.close()

def _mark_processed(upd_id: str):
    if not upd_id: return
    with _tx() as con:
        con.execute("INSERT OR IGNORE INTO processed (update_id, seen_at) VALUES (?,?)",
                    (str(upd_id), now_ts()))

# ========= Timeline / N-gram / Feedback =========
def append_seq(seq: List[int]):
    if not seq: return
    with _tx() as con:
        for n in seq:
            con.execute("INSERT INTO timeline (created_at, number) VALUES (?,?)",
                        (now_ts(), int(n)))
    _update_ngrams()

def get_tail(limit:int=None) -> List[int]:
    if limit is None: limit = HISTORY_WINDOW
    con = _connect()
    rows = con.execute("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    con.close()
    return [int(r["number"]) for r in rows][::-1]

def _update_ngrams(decay: float=None, max_n:int=5, window:int=None):
    if decay is None: decay = NGRAM_DECAY
    if window is None: window = HISTORY_WINDOW
    tail = get_tail(window)
    if len(tail) < 2: return
    with _tx() as con:
        for t in range(1, len(tail)):
            nxt = int(tail[t])
            dist = (len(tail)-1) - t
            w = float(decay) ** float(dist)
            for n in range(2, max_n+1):
                if t-(n-1) < 0: break
                ctx = tail[t-(n-1):t]
                ctx_key = ",".join(str(x) for x in ctx)
                con.execute("""
                  INSERT INTO ngram (n, ctx, nxt, w)
                  VALUES (?,?,?,?)
                  ON CONFLICT(n, ctx, nxt) DO UPDATE SET w = w + excluded.w
                """, (n, ctx_key, nxt, w))

def _prob_from_ngrams(ctx: List[int], cand: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    con = _connect()
    row_tot = con.execute("SELECT SUM(w) AS s FROM ngram WHERE n=? AND ctx=?", (n, ctx_key)).fetchone()
    tot = float((row_tot["s"] or 0.0) if row_tot else 0.0)
    if tot <= 0: con.close(); return 0.0
    row_c = con.execute("SELECT w FROM ngram WHERE n=? AND ctx=? AND nxt=?", (n, ctx_key, int(cand))).fetchone()
    w = float((row_c["w"] or 0.0) if row_c else 0.0)
    con.close()
    return max(0.0, w / tot)

def _ctx_key(ctx: List[int]) -> str:
    return ",".join(str(x) for x in ctx) if ctx else ""

def _feedback_upsert(n:int, ctx_key:str, nxt:int, delta:float):
    with _tx() as con:
        con.execute("UPDATE feedback SET w = w * ?", (float(FEEDBACK_DECAY),))
        con.execute("""
          INSERT INTO feedback (n, ctx, nxt, w)
          VALUES (?,?,?,?)
          ON CONFLICT(n, ctx, nxt) DO UPDATE SET w = w + excluded.w
        """, (n, ctx_key, int(nxt), float(delta)))

def _feedback_prob(n:int, ctx: List[int], cand:int) -> float:
    if not ctx: return 0.0
    key = _ctx_key(ctx)
    con = _connect()
    row_tot = con.execute("SELECT SUM(w) AS s FROM feedback WHERE n=? AND ctx=?", (n, key)).fetchone()
    tot = float((row_tot["s"] or 0.0) if row_tot else 0.0)
    if tot <= 0: con.close(); return 0.0
    row_c = con.execute("SELECT w FROM feedback WHERE n=? AND ctx=? AND nxt=?", (n, key, int(cand))).fetchone()
    w = float((row_c["w"] or 0.0) if row_c else 0.0)
    con.close()
    return max(0.0, w / (abs(tot) if abs(tot) > 0 else 1e-9))

# ========= Pending (G0 only) =========
def get_open_pending() -> Optional[sqlite3.Row]:
    con = _connect()
    row = con.execute("SELECT * FROM pending WHERE open=1 ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return row

def _open_pending(suggested:int) -> bool:
    with _tx() as con:
        row = con.execute("SELECT 1 FROM pending WHERE open=1 LIMIT 1").fetchone()
        if row: return False
        con.execute("""INSERT INTO pending (created_at, suggested, open, seen, opened_at, suggest_msg_id)
                       VALUES (?,?,?,?,?,NULL)""", (now_ts(), int(suggested), 1, "", now_ts()))
        return True

def _append_seen(row: sqlite3.Row, nums: List[int]):
    seen = (row["seen"] or "").strip()
    cur = [s for s in seen.split("-") if s]
    for n in nums:
        if len(cur) >= 1: break  # G0 only: só 1 observado
        cur.append(str(int(n)))
    seen_txt = "-".join(cur[:1])
    with _tx() as con:
        con.execute("UPDATE pending SET seen=? WHERE id=?", (seen_txt, int(row["id"])))

def _save_suggest_msg_id(msg_id: Optional[int]):
    if msg_id is None: return
    with _tx() as con:
        con.execute("UPDATE pending SET suggest_msg_id=? WHERE open=1 ORDER BY id DESC LIMIT 1",
                    (int(msg_id),))

def _ngram_snapshot_text(suggested:int) -> str:
    tail = get_tail(HISTORY_WINDOW)
    post = _post_ngram_feedback(tail, None)
    def pct(x: float) -> str:
        try: return f"{x*100:.1f}%"
        except Exception: return "0.0%"
    p1 = pct(post.get(1,0.0)); p2 = pct(post.get(2,0.0))
    p3 = pct(post.get(3,0.0)); p4 = pct(post.get(4,0.0))
    conf = pct(post.get(int(suggested),0.0))
    amostra = len(tail)
    return (
        f"📈 Amostra: {amostra} • Conf: {conf}\n\n"
        f"🔎 E1(n-gram+fb): 1 {p1} | 2 {p2} | 3 {p3} | 4 {p4}"
    )

def _close_if_ready():
    row = get_open_pending()
    if not row: return None
    seen = (row["seen"] or "").strip()
    parts = [p for p in seen.split("-") if p]
    if len(parts) >= 1:
        with _tx() as con:
            con.execute("UPDATE pending SET open=0 WHERE id=?", (int(row["id"]),))
        # feedback leve no último contexto
        try:
            tail = get_tail(5)
            true1 = int(parts[0])
            _feedback_upsert(2, _ctx_key(tail[-1:]), true1, +float(FEEDBACK_REINFORCE))
        except Exception:
            pass
        suggested = int(row["suggested"] or 0)
        our_disp = suggested if suggested == int(parts[0]) else "X"
        outcome  = "GREEN" if suggested == int(parts[0]) else "LOSS"
        bump_score(outcome)
        snap = _ngram_snapshot_text(suggested)
        msg = (
            f"{'🟢' if outcome=='GREEN' else '🔴'} <b>{outcome}</b> — finalizado "
            f"(G0, nosso={our_disp}, observados={parts[0]}).\n"
            f"📊 Geral: {score_text()}\n\n{snap}"
        )
        # pegar a msg da sugestão para fazer reply
        reply_to = None
        try:
            con = _connect()
            r2 = con.execute("SELECT suggest_msg_id FROM pending WHERE id=?", (int(row["id"]),)).fetchone()
            con.close()
            reply_to = int(r2["suggest_msg_id"] or 0) if r2 and r2["suggest_msg_id"] else None
        except Exception:
            pass
        return {"closed": True, "seen": parts[0], "msg": msg, "suggested": suggested, "reply_to": reply_to}
    return None

# ========= Telegram =========
TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

async def tg_send_text(chat_id: str, text: str, parse: str = "HTML", reply_to: int = None):
    """Envia mensagem; se reply_to for dado, responde àquela msg. Retorna message_id ou None."""
    if not TG_BOT_TOKEN: return None
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse,
        "disable_web_page_preview": True
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = int(reply_to)
        payload["allow_sending_without_reply"] = True
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
            data = resp.json()
            return (data.get("result") or {}).get("message_id")
    except Exception:
        return None

# ========= Parsers =========
ENTRY_RX = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
SEQ_RX   = re.compile(r"Sequ[eê]ncia:\s*([^\n\r]+)", re.I)
AFTER_RX = re.compile(r"ap[oó]s\s+o\s+([1-4])", re.I)
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
    mseq = SEQ_RX.search(t); seq=[]
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
        return [int(x) for x in nums][:1]  # G0: só 1
    nums = ANY_14_RX.findall(t)
    return [int(x) for x in nums][:1]

def parse_analise_seq(text: str) -> List[int]:
    if not ANALISANDO_RX.search(_normalize_keycaps(text or "")): return []
    mseq = SEQ_RX.search(_normalize_keycaps(text or ""))
    if not mseq: return []
    return [int(x) for x in re.findall(r"[1-4]", mseq.group(1))]

# ========= Especialistas (E1..E10) =========
CANDS = [1,2,3,4]

def _post_ngram_feedback(tail: List[int], after: Optional[int]) -> Dict[int,float]:
    if not tail: return {c:0.25 for c in CANDS}
    if after is not None and after in tail:
        idxs = [i for i,v in enumerate(tail) if v == after]; i = idxs[-1]
        ctxs = [tail[max(0,i-3):i+1], tail[max(0,i-2):i+1], tail[max(0,i-1):i+1], tail[i:i+1]]
    else:
        ctxs = [tail[-4:], tail[-3:], tail[-2:], tail[-1:]]
    W4,W3,W2,W1 = 0.42,0.30,0.18,0.10
    scores = {c:0.0 for c in CANDS}
    for c in CANDS:
        if len(ctxs[0])==4: scores[c] += W4 * _prob_from_ngrams(ctxs[0][:-1], c)
        if len(ctxs[1])==3: scores[c] += W3 * _prob_from_ngrams(ctxs[1][:-1], c)
        if len(ctxs[2])==2: scores[c] += W2 * _prob_from_ngrams(ctxs[2][:-1], c)
        if len(ctxs[3])==1: scores[c] += W1 * _prob_from_ngrams(ctxs[3][:-1], c)
        if len(ctxs[0])>=2: scores[c] += 0.40 * _feedback_prob(4, ctxs[0][:-1], c)
        if len(ctxs[1])>=1: scores[c] += 0.30 * _feedback_prob(3, ctxs[1][:-1], c)
        if len(ctxs[2])>=1: scores[c] += 0.18 * _feedback_prob(2, ctxs[2][:-1], c)
        if len(ctxs[3])>=1: scores[c] += 0.10 * _feedback_prob(1, ctxs[3][:-1], c)
    return _norm(scores)

def _post_freq_k(tail: List[int], k:int) -> Dict[int,float]:
    if not tail: return {c:0.25 for c in CANDS}
    win = tail[-k:] if len(tail)>=k else tail
    tot = max(1, len(win))
    return _norm({c: win.count(c)/tot for c in CANDS})

def _post_entropy_balancer(tail: List[int]) -> Dict[int,float]:
    f60 = _post_freq_k(tail, 60)
    H = -sum(p*math.log(p+1e-12,4) for p in f60.values())
    mix = 0.35 if H < 0.6 else 0.15
    uni = {c:0.25 for c in CANDS}
    return _norm({c: (1-mix)*f60[c] + mix*uni[c] for c in CANDS})

def _post_markov_1(tail: List[int]) -> Dict[int,float]:
    if len(tail) < 2: return {c:0.25 for c in CANDS}
    counts = {c:{d:1.0 for d in CANDS} for c in CANDS}
    for i in range(1, len(tail)):
        prev, nxt = tail[i-1], tail[i]
        counts[prev][nxt] += 1.0
    last = tail[-1]
    return _norm(counts[last])

def _post_exp_decay_freq(tail: List[int]) -> Dict[int,float]:
    if not tail: return {c:0.25 for c in CANDS}
    lam = 0.95; score = {c:0.0 for c in CANDS}; w = 1.0
    for v in reversed(tail):
        score[v] += w; w *= lam
    return _norm(score)

def _post_pair_table(tail: List[int]) -> Dict[int,float]:
    if len(tail) < 2: return {c:0.25 for c in CANDS}
    pair = {}
    for i in range(1, len(tail)):
        a, b = tail[i-1], tail[i]
        pair.setdefault((a,b), {c:1.0 for c in CANDS})
        if i+1 < len(tail):
            nxt = tail[i+1]
            pair[(a,b)][nxt] = pair[(a,b)].get(nxt,1.0)+1.0
    a, b = tail[-2], tail[-1]
    return _norm(pair.get((a,b), {c:1.0 for c in CANDS}))

def _post_run_length(tail: List[int]) -> Dict[int,float]:
    if not tail: return {c:0.25 for c in CANDS}
    last = tail[-1]; run = 1
    for i in range(len(tail)-2, -1, -1):
        if tail[i]==last: run+=1
        else: break
    d = {c:0.25 for c in CANDS}
    d[last] = max(0.01, 0.40 - 0.08*run)
    rest = 1.0 - d[last]; others = [c for c in CANDS if c!=last]
    for c in others: d[c] = rest/len(others)
    return _norm(d)

def _post_recency_penalty(tail: List[int]) -> Dict[int,float]:
    if not tail: return {c:0.25 for c in CANDS}
    last_pos = {c: -9999 for c in CANDS}
    for idx, v in enumerate(tail): last_pos[v] = idx
    n = len(tail)-1
    gaps = {c: (n - last_pos[c]) for c in CANDS}
    return _softmax(gaps, t=3.0)

def _post_meta_sharpen(prev_posts: List[Dict[int,float]]) -> Dict[int,float]:
    avg = {c:0.0 for c in CANDS}
    for p in prev_posts:
        for c in CANDS:
            avg[c] += p.get(c, 0.0)
    for c in CANDS: avg[c] /= max(1, len(prev_posts))
    return _softmax(avg, t=META_TEMP)

def _ensemble_10(tail: List[int], after: Optional[int]) -> Tuple[int, Dict[int,float], Dict[str,Dict[int,float]]]:
    posts = {}
    posts["E01_ngram+fb"] = _post_ngram_feedback(tail, after)
    posts["E02_freq60"]   = _post_freq_k(tail, 60)
    posts["E03_freq300"]  = _post_freq_k(tail, 300)
    posts["E04_entropy"]  = _post_entropy_balancer(tail)
    posts["E05_markov1"]  = _post_markov_1(tail)
    posts["E06_decay"]    = _post_exp_decay_freq(tail)
    posts["E07_pairTbl"]  = _post_pair_table(tail)
    posts["E08_runlen"]   = _post_run_length(tail)
    posts["E09_recency"]  = _post_recency_penalty(tail)
    posts["E10_meta"]     = _post_meta_sharpen(list(posts.values()))

    weights = {k: 1.0 for k in posts.keys()}  # homogêneo/estável

    agg = {c:0.0 for c in CANDS}
    for name, dist in posts.items():
        w = weights[name]
        for c in CANDS:
            agg[c] += w * dist.get(c, 0.0)

    agg = _softmax(agg, t=ENSEMBLE_TEMP)
    best = max(agg.items(), key=lambda kv: kv[1])[0]
    return best, agg, posts

# ========= Decisão G0 =========
def choose_g0(after: Optional[int]) -> Tuple[int, float, float, Dict[int,float], Dict[str,Dict[int,float]]]:
    tail = get_tail(HISTORY_WINDOW)
    best, post, posts_all = _ensemble_10(tail, after)
    conf = float(post.get(best, 0.0))
    gap  = _gap_top2(post)
    return best, conf, gap, post, posts_all

# ========= Rotas =========
@app.get("/health")
async def health():
    pend = get_open_pending()
    return {"ok": True,
            "db": DB_PATH,
            "pending_open": bool(pend),
            "pending_seen": (pend["seen"] if pend else ""),
            "time": ts_str(),
            "tz": TZ_NAME,
            "history_window": HISTORY_WINDOW}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()

    # DEDUPE
    upd_id = str(data.get("update_id", "")) if isinstance(data, dict) else ""
    if _is_processed(upd_id):
        return {"ok": True, "skipped": "duplicate_update"}
    _mark_processed(upd_id)

    msg = data.get("channel_post") or data.get("message") \
        or data.get("edited_channel_post") or data.get("edited_message") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    # Filtro de origem
    if SOURCE_CHANNEL and not BYPASS_SOURCE and chat_id != str(SOURCE_CHANNEL):
        if DEBUG_MSG:
            await tg_send_text(TARGET_CHANNEL, f"DEBUG: Ignorando chat {chat_id}. Fonte esperada: {SOURCE_CHANNEL}")
        return {"ok": True, "skipped": "outro_chat"}
    if not text:
        return {"ok": True, "skipped": "sem_texto"}

    # 1) “ANALISANDO”: aprende e pode abrir se não houver pendência
    if ANALISANDO_RX.search(_normalize_keycaps(text)):
        seq = parse_analise_seq(text)
        if seq: append_seq(seq)
        if not get_open_pending():
            mafter = AFTER_RX.search(_normalize_keycaps(text or ""))
            after = int(mafter.group(1)) if mafter else None
            best, conf, gap, _, _ = choose_g0(after)
            if _open_pending(best):
                msg_id = await tg_send_text(
                    TARGET_CHANNEL,
                    (f"🤖 <b>IA SUGERE — {best}</b>\n"
                     f"🧩 <b>Padrão:</b> GEN (after_close)\n"
                     f"📊 <b>Conf:</b> {conf*100:.2f}% | <b>Amostra≈</b>{len(get_tail(HISTORY_WINDOW))} | <b>gap≈</b>{gap*100:.1f}pp\n"
                     f"🧠 <b>Modo:</b> E10_meta")
                )
                _save_suggest_msg_id(msg_id)
                return {"ok": True, "posted": True, "best": best, "conf": conf}
        return {"ok": True, "analise": len(seq)}

    # 2) Fechamento (GREEN/LOSS) — G0 pega só 1 número observado
    if GREEN_RX.search(text) or LOSS_RX.search(text):
        pend = get_open_pending()
        if pend:
            nums = parse_close_numbers(text)  # só 1
            if nums:
                _append_seen(pend, nums)
            r = _close_if_ready()
            if r and r.get("closed"):
                # (1) resultado em reply à sugestão correspondente
                await tg_send_text(TARGET_CHANNEL, r["msg"], reply_to=r.get("reply_to"))
                # (2) opcional: abrir já o próximo após fechar
                if OPEN_AFTER_CLOSE:
                    m_after = AFTER_RX.search(_normalize_keycaps(text or ""))
                    after = int(m_after.group(1)) if m_after else None
                    best2, conf2, gap2, _, _ = choose_g0(after)
                    if _open_pending(best2):
                        msg_id2 = await tg_send_text(
                            TARGET_CHANNEL,
                            (f"🤖 <b>IA SUGERE — {best2}</b>\n"
                             f"🧩 <b>Padrão:</b> GEN (after_close)\n"
                             f"📊 <b>Conf:</b> {conf2*100:.2f}% | <b>Amostra≈</b>{len(get_tail(HISTORY_WINDOW))} | <b>gap≈</b>{gap2*100:.1f}pp\n"
                             f"🧠 <b>Modo:</b> E10_meta")
                        )
                        _save_suggest_msg_id(msg_id2)
                return {"ok": True, "closed": True, "seen": r["seen"]}
        return {"ok": True, "noted_close": True}

    # 3) ENTRADA CONFIRMADA — gera decisão G0
    parsed = parse_entry_text(text)
    if not parsed:
        if DEBUG_MSG:
            await tg_send_text(TARGET_CHANNEL, "DEBUG: mensagem não reconhecida.")
        return {"ok": True, "skipped": "nao_eh_entrada_confirmada"}

    # aprende sequência publicada
    seq = parsed["seq"] or []
    if seq: append_seq(seq)

    after = parsed["after"]
    best, conf, gap, _, _ = choose_g0(after)

    if not get_open_pending():
        if _open_pending(best):
            msg_id3 = await tg_send_text(
                TARGET_CHANNEL,
                (f"🤖 <b>IA SUGERE — {best}</b>\n"
                 f"🧩 <b>Padrão:</b> GEN\n"
                 f"📊 <b>Conf:</b> {conf*100:.2f}% | <b>Amostra≈</b>{len(get_tail(HISTORY_WINDOW))} | <b>gap≈</b>{gap*100:.1f}pp\n"
                 f"🧠 <b>Modo:</b> E10_meta")
            )
            _save_suggest_msg_id(msg_id3)
            return {"ok": True, "posted": True, "best": best, "conf": conf}

    return {"ok": True, "skipped": "pending_already_open"}