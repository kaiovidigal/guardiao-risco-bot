#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
webhook_app.py — G0 only — 20 especialistas (E1..E20)
- Sem gales (G1..G6): decisão única (G0) por consenso de 20 especialistas
- Configurações embutidas (sem ENV)
- Dedupe por update_id (tabela processed)
- Filtro por SOURCE_CHANNEL
- Encaminha SUGESTÃO para TARGET_CHANNEL
- Fecha pendências só com o primeiro observado (G0), sem lógica de G1/G2
- DB SQLite robusto com WAL + busy_timeout

Rotas:
  GET  /                -> ok
  GET  /health          -> status
  POST /webhook/meusegredo123  -> endpoint do Telegram
"""

import os, re, time, sqlite3, math, json, random
from contextlib import contextmanager
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone

# ========= CONFIG FIXA =========
TG_BOT_TOKEN   = "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4"
WEBHOOK_TOKEN  = "meusegredo123"
TARGET_CHANNEL = "-1002796105884"
SOURCE_CHANNEL = "-1002810508717"
DB_PATH        = "/var/data/data.db"
TZ_NAME        = "America/Sao_Paulo"

DEBUG_MSG      = False     # enviar mensagens de debug no canal destino
BYPASS_SOURCE  = False     # se True, não filtra SOURCE_CHANNEL

# ========= Imports web =========
import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= Timezone =========
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(TZ_NAME)
except Exception:
    _TZ = timezone.utc

# ========= App =========
app = FastAPI(title="guardiao-g0-20x", version="1.0.0")

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
    cur.execute("""CREATE TABLE IF NOT EXISTS pending(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER,
        suggested INTEGER,
        open INTEGER DEFAULT 1,
        seen TEXT,
        opened_at INTEGER
    )""")
    con.commit(); con.close()
_ensure_tables()

def _is_processed(upd_id: str) -> bool:
    if not upd_id: return False
    con = _connect()
    row = con.execute("SELECT 1 FROM processed WHERE update_id=?", (upd_id,)).fetchone()
    con.close()
    return bool(row)

def _mark_processed(upd_id: str):
    if not upd_id: return
    with _tx() as con:
        con.execute("INSERT OR IGNORE INTO processed (update_id, seen_at) VALUES (?,?)",
                    (upd_id, now_ts()))

def append_seq(seq: List[int]):
    if not seq: return
    with _tx() as con:
        for n in seq:
            con.execute("INSERT INTO timeline (created_at, number) VALUES (?,?)",
                        (now_ts(), int(n)))
    _update_ngrams()

def get_tail(limit:int=400) -> List[int]:
    con = _connect()
    rows = con.execute("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [int(r["number"]) for r in rows][::-1]

def _update_ngrams(decay: float=0.980, max_n:int=5, window:int=400):
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
        con.execute("UPDATE feedback SET w = w * 0.995")
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
        con.execute("""INSERT INTO pending (created_at, suggested, open, seen, opened_at)
                       VALUES (?,?,?,?,?)""", (now_ts(), int(suggested), 1, "", now_ts()))
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

def _close_if_ready():
    row = get_open_pending()
    if not row: return None
    seen = (row["seen"] or "").strip()
    parts = [p for p in seen.split("-") if p]
    if len(parts) >= 1:
        with _tx() as con:
            con.execute("UPDATE pending SET open=0 WHERE id=?", (int(row["id"]),))
        # feedback leve: reforça o 1º observado
        try:
            tail = get_tail(5)
            for n in (1,2,3,4):
                _feedback_upsert(2, _ctx_key(tail[-1:]), n, +0.0)  # decaimento
            true1 = int(parts[0])
            _feedback_upsert(2, _ctx_key(tail[-1:]), true1, +0.8)
        except Exception:
            pass
        return {"closed": True, "seen": parts[0]}
    return None

# ========= Telegram =========
TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

async def tg_send_text(chat_id: str, text: str, parse: str = "HTML"):
    if not TG_BOT_TOKEN: return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{TELEGRAM_API}/sendMessage",
                              json={"chat_id": chat_id, "text": text, "parse_mode": parse,
                                    "disable_web_page_preview": True})
    except Exception:
        pass

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
        return [int(x) for x in nums][:1]  # G0: só 1
    nums = ANY_14_RX.findall(t)
    return [int(x) for x in nums][:1]

def parse_analise_seq(text: str) -> List[int]:
    if not ANALISANDO_RX.search(_normalize_keycaps(text or "")): return []
    mseq = SEQ_RX.search(_normalize_keycaps(text or ""))
    if not mseq: return []
    return [int(x) for x in re.findall(r"[1-4]", mseq.group(1))]

# ========= Especialistas (E1..E20) =========
CANDS = [1,2,3,4]

def _post_ngram_feedback(tail: List[int], after: Optional[int]) -> Dict[int,float]:
    # usa 1..4-gram + feedback nas janelas
    if not tail: return {c:0.25 for c in CANDS}
    ctxs = []
    if after is not None and after in tail:
        idxs = [i for i,v in enumerate(tail) if v == after]
        i = idxs[-1]
        ctxs = [tail[max(0,i-3):i+1], tail[max(0,i-2):i+1], tail[max(0,i-1):i+1], tail[i:i+1]]
    else:
        ctxs = [tail[-4:], tail[-3:], tail[-2:], tail[-1:]]
    W4, W3, W2, W1 = 0.42, 0.30, 0.18, 0.10
    scores = {c:0.0 for c in CANDS}
    for c in CANDS:
        if len(ctxs[0])==4: scores[c] += W4 * _prob_from_ngrams(ctxs[0][:-1], c)
        if len(ctxs[1])==3: scores[c] += W3 * _prob_from_ngrams(ctxs[1][:-1], c)
        if len(ctxs[2])==2: scores[c] += W2 * _prob_from_ngrams(ctxs[2][:-1], c)
        if len(ctxs[3])==1: scores[c] += W1 * _prob_from_ngrams(ctxs[3][:-1], c)
        # feedback leve
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
    # se distribuição muito desbalanceada, compensa um pouco
    f60 = _post_freq_k(tail, 60)
    H = -sum(p*math.log(p+1e-12, 4) for p in f60.values())
    mix = 0.35 if H < 0.6 else 0.15
    uni = {c:0.25 for c in CANDS}
    return _norm({c: (1-mix)*f60[c] + mix*uni[c] for c in CANDS})

def _post_fibo_like(tail: List[int]) -> Dict[int,float]:
    # mapeia (a,b) -> (a+b) mod 4 (1..4)
    if len(tail) < 2:
        return {c:0.25 for c in CANDS}
    a, b = tail[-2], tail[-1]
    raw = (a + b) % 4
    pred = 4 if raw==0 else raw
    d = {c:0.05 for c in CANDS}; d[pred] = 0.85
    return _norm(d)

def _post_paridade(tail: List[int]) -> Dict[int,float]:
    if not tail: return {c:0.25 for c in CANDS}
    last = tail[-1]
    want_even = (last % 2 != 0)  # alterna paridade
    d = {c:0.15 for c in CANDS}
    for c in CANDS:
        if (c%2==0) == want_even:
            d[c] += 0.35
    return _norm(d)

def _post_run_length(tail: List[int]) -> Dict[int,float]:
    if not tail: return {c:0.25 for c in CANDS}
    last = tail[-1]
    run = 1
    for i in range(len(tail)-2, -1, -1):
        if tail[i]==last: run+=1
        else: break
    d = {c:0.25 for c in CANDS}
    # quanto maior o run, mais penaliza repetir
    d[last] = max(0.01, 0.40 - 0.08*run)
    # distribui o restante
    rest = 1.0 - d[last]
    others = [c for c in CANDS if c!=last]
    for c in others: d[c] = rest/len(others)
    return _norm(d)

def _post_markov_1(tail: List[int]) -> Dict[int,float]:
    if len(tail) < 2: return {c:0.25 for c in CANDS}
    # transições de 1ª ordem
    counts = {c:{d:1.0 for d in CANDS} for c in CANDS}  # Laplace
    for i in range(1, len(tail)):
        prev, nxt = tail[i-1], tail[i]
        counts[prev][nxt] += 1.0
    last = tail[-1]
    row = counts[last]
    return _norm(row)

def _post_recency_penalty(tail: List[int]) -> Dict[int,float]:
    # favorece quem está mais “tempo sem sair”
    if not tail: return {c:0.25 for c in CANDS}
    last_pos = {c: -9999 for c in CANDS}
    for idx, v in enumerate(tail):
        last_pos[v] = idx
    n = len(tail)-1
    gaps = {c: (n - last_pos[c]) for c in CANDS}
    return _softmax(gaps, t=3.0)

def _post_exp_decay_freq(tail: List[int]) -> Dict[int,float]:
    if not tail: return {c:0.25 for c in CANDS}
    lam = 0.95
    score = {c:0.0 for c in CANDS}
    w = 1.0
    for v in reversed(tail):
        score[v] += w
        w *= lam
    return _norm(score)

def _post_least_freq_short(tail: List[int]) -> Dict[int,float]:
    f = _post_freq_k(tail, 30)
    # inverter preferência: dá mais peso aos menos frequentes
    inv = {c: (1.001 - f[c]) for c in CANDS}
    return _norm(inv)

def _post_streak_breaker(tail: List[int]) -> Dict[int,float]:
    if not tail: return {c:0.25 for c in CANDS}
    last = tail[-1]
    d = {c:0.24 for c in CANDS}
    d[last] = 0.05
    return _norm(d)

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
    dist = pair.get((a,b), {c:1.0 for c in CANDS})
    return _norm(dist)

def _post_cycle_guess(tail: List[int]) -> Dict[int,float]:
    # tenta seguir 1→2→3→4→1...
    if not tail: return {c:0.25 for c in CANDS}
    pred = (tail[-1] % 4) + 1
    d = {c:0.09 for c in CANDS}; d[pred] = 0.73
    return _norm(d)

def _post_mirror(tail: List[int]) -> Dict[int,float]:
    # mapeamento 1↔3, 2↔4
    if not tail: return {c:0.25 for c in CANDS}
    m = {1:3, 3:1, 2:4, 4:2}
    pred = m.get(tail[-1], 1)
    d = {c:0.10 for c in CANDS}; d[pred] = 0.70
    return _norm(d)

def _post_feedback_bias(tail: List[int]) -> Dict[int,float]:
    # usa feedback n=2 no último contexto
    if not tail: return {c:0.25 for c in CANDS}
    ctx = tail[-1:]
    score = {c: 0.25 + 0.5*_feedback_prob(2, ctx, c) for c in CANDS}
    return _norm(score)

def _post_bayes_dirichlet(tail: List[int]) -> Dict[int,float]:
    alpha = {c:1.0 for c in CANDS}
    for v in tail[-120:]:
        alpha[v] += 1.0
    s = sum(alpha.values())
    return {c: alpha[c]/s for c in CANDS}

def _post_time_mod(tail: List[int]) -> Dict[int,float]:
    # minuto do dia mod 4
    m = int(datetime.now(_TZ).strftime("%M"))
    pred = (m % 4) + 1
    d = {c:0.10 for c in CANDS}; d[pred] = 0.70
    return _norm(d)

def _post_anti_entropy(tail: List[int]) -> Dict[int,float]:
    f = _post_freq_k(tail, 80)
    H = -sum(p*math.log(p+1e-12,4) for p in f.values())
    if H > 0.9:
        # tudo muito “flat”: evita repetir o último
        d = {c:0.26 for c in CANDS}
        if tail: d[tail[-1]] = 0.05
        return _norm(d)
    return f

def _post_meta_sharpen(prev_posts: List[Dict[int,float]]) -> Dict[int,float]:
    # média e depois “sharpen” (temperatura)
    avg = {c:0.0 for c in CANDS}
    for p in prev_posts:
        for c in CANDS:
            avg[c] += p.get(c, 0.0)
    for c in CANDS: avg[c] /= max(1, len(prev_posts))
    return _softmax(avg, t=0.6)

def _ensemble_20(tail: List[int], after: Optional[int]) -> Tuple[int, Dict[int,float], Dict[str,Dict[int,float]]]:
    posts = {}
    posts["E1_ngram"]      = _post_ngram_feedback(tail, after)
    posts["E2_freq60"]     = _post_freq_k(tail, 60)
    posts["E3_freq300"]    = _post_freq_k(tail, 300)
    posts["E4_entropy"]    = _post_entropy_balancer(tail)
    posts["E5_fibo"]       = _post_fibo_like(tail)
    posts["E6_paridade"]   = _post_paridade(tail)
    posts["E7_runlen"]     = _post_run_length(tail)
    posts["E8_markov1"]    = _post_markov_1(tail)
    posts["E9_recency"]    = _post_recency_penalty(tail)
    posts["E10_decay"]     = _post_exp_decay_freq(tail)
    posts["E11_leastS"]    = _post_least_freq_short(tail)
    posts["E12_streakBr"]  = _post_streak_breaker(tail)
    posts["E13_pairTbl"]   = _post_pair_table(tail)
    posts["E14_cycle"]     = _post_cycle_guess(tail)
    posts["E15_mirror"]    = _post_mirror(tail)
    posts["E16_fbBias"]    = _post_feedback_bias(tail)
    posts["E17_bayes"]     = _post_bayes_dirichlet(tail)
    posts["E18_timeMod"]   = _post_time_mod(tail)
    posts["E19_antiH"]     = _post_anti_entropy(tail)
    # E20: meta do conjunto acima
    meta = _post_meta_sharpen(list(posts.values()))
    posts["E20_meta"] = meta

    # pesos iniciais (podem ser todos 1.0 para neutralidade)
    weights = {k: 1.0 for k in posts.keys()}

    agg = {c:0.0 for c in CANDS}
    for name, dist in posts.items():
        w = weights[name]
        for c in CANDS:
            agg[c] += w * dist.get(c, 0.0)

    agg = _softmax(agg, t=0.75)  # leve “sharpen”
    best = max(agg.items(), key=lambda kv: kv[1])[0]
    return best, agg, posts

# ========= Decisão G0 =========
def choose_g0(after: Optional[int]) -> Tuple[int, float, Dict[int,float], Dict[str,Dict[int,float]]]:
    tail = get_tail(400)
    best, post, posts_all = _ensemble_20(tail, after)
    conf = float(post.get(best, 0.0))
    return best, conf, post, posts_all

# ========= Rotas =========
@app.get("/")
async def root():
    return {"ok": True, "service": "guardiao-g0-20x", "time": ts_str()}

@app.get("/health")
async def health():
    pend = get_open_pending()
    return {
        "ok": True,
        "db": DB_PATH,
        "pending_open": bool(pend),
        "pending_seen": (pend["seen"] if pend else ""),
        "time": ts_str(),
        "tz": TZ_NAME,
    }

@app.post(f"/webhook/{WEBHOOK_TOKEN}")
async def webhook(request: Request):
    data = await request.json()
    # Dedupe
    upd_id = ""
    if isinstance(data, dict):
        upd_id = str(data.get("update_id", "") or "")
    if _is_processed(upd_id):
        return {"ok": True, "skipped": "duplicate_update"}
    if upd_id:
        _mark_processed(upd_id)

    # Mensagem
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

    # 1) “ANALISANDO”: aprende a sequência (memória) e pode sugerir
    if ANALISANDO_RX.search(_normalize_keycaps(text)):
        seq = parse_analise_seq(text)
        if seq: append_seq(seq)
        # abre sugestão se não houver pendência
        if not get_open_pending():
            mafter = AFTER_RX.search(_normalize_keycaps(text or ""))
            after = int(mafter.group(1)) if mafter else None
            best, conf, post, _ = choose_g0(after)
            if _open_pending(best):
                await tg_send_text(
                    TARGET_CHANNEL,
                    f"🤖 <b>IA (G0)</b> sugere <b>{best}</b> • Conf≈<b>{conf*100:.1f}%</b>"
                )
                return {"ok": True, "posted": True, "best": best, "conf": conf}
        return {"ok": True, "analise": len(seq)}

    # 2) Fechamento (GREEN/LOSS) — G0 só usa o 1º número visto
    if GREEN_RX.search(text) or LOSS_RX.search(text):
        pend = get_open_pending()
        if pend:
            nums = parse_close_numbers(text)  # só 1
            if nums:
                _append_seen(pend, nums)
            r = _close_if_ready()
            if r and r.get("closed"):
                await tg_send_text(TARGET_CHANNEL, f"📌 Fechado (G0) — observado <b>{r['seen']}</b>.")
                return {"ok": True, "closed": True, "seen": r["seen"]}
        return {"ok": True, "noted_close": True}

    # 3) ENTRADA CONFIRMADA — gatilha a decisão G0
    parsed = parse_entry_text(text)
    if not parsed:
        if DEBUG_MSG:
            await tg_send_text(TARGET_CHANNEL, "DEBUG: mensagem não reconhecida.")
        return {"ok": True, "skipped": "nao_eh_entrada_confirmada"}

    # aprende qualquer “Sequência:” publicada
    seq = parsed["seq"] or []
    if seq: append_seq(seq)

    after = parsed["after"]
    best, conf, post, _ = choose_g0(after)

    if not get_open_pending():
        if _open_pending(best):
            await tg_send_text(
                TARGET_CHANNEL,
                f"🤖 <b>IA (G0)</b> sugere <b>{best}</b> • Conf≈<b>{conf*100:.1f}%</b>"
            )
            return {"ok": True, "posted": True, "best": best, "conf": conf}

    # se já tem pendência aberta, só retorna
    return {"ok": True, "skipped": "pending_already_open"}