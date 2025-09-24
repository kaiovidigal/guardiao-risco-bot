#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, time, json, sqlite3, asyncio, pickle
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= ENV =========
env = lambda k, d=None: os.getenv(k, d).strip() if os.getenv(k) is not None else (d if d is not None else "")
to_f = lambda k, d: float(os.getenv(k, d))
to_i = lambda k, d: int(os.getenv(k, d))

TG_BOT_TOKEN   = env("TG_BOT_TOKEN")
WEBHOOK_TOKEN  = env("WEBHOOK_TOKEN", "meusegredo123")
TARGET_CHANNEL = env("TARGET_CHANNEL", "-1002796105884")
SOURCE_CHANNEL = env("SOURCE_CHANNEL", "-1002810508717")
DB_PATH        = env("DB_PATH", "/var/data/main.sqlite")
RESET_TZ       = env("RESET_TZ", "America/Sao_Paulo")

MODEL_DIR      = env("MODEL_DIR", "/var/data")
MODEL_PATH     = env("MODEL_PATH", os.path.join(MODEL_DIR, "model_GLOBAL.pkl"))
ML_MIN_PROBA   = to_f("ML_MIN_PROBA", 0.78)
ML_GAP_MIN     = to_f("ML_GAP_MIN", 0.08)
ENSEMBLE_MODE  = env("ENSEMBLE_MODE", "blend")  # "gate" | "blend" | "ml_only"

REPORT_EVERY_SEC   = to_i("REPORT_EVERY_SEC", 300)
GOOD_DAY_THRESHOLD = to_f("GOOD_DAY_THRESHOLD", 0.70)
BAD_DAY_THRESHOLD  = to_f("BAD_DAY_THRESHOLD", 0.40)

SHORT_WINDOW   = to_i("SHORT_WINDOW", 40)
LONG_WINDOW    = to_i("LONG_WINDOW", 600)
CONF_SHORT_MIN = to_f("CONF_SHORT_MIN", 0.49)
CONF_LONG_MIN  = to_f("CONF_LONG_MIN", 0.59)
GAP_MIN        = to_f("GAP_MIN", 0.025)
FINAL_TIMEOUT  = to_i("FINAL_TIMEOUT", 45)

TRAIN_ON_START        = env("TRAIN_ON_START", "false").lower() in ("1","true","yes","y")
MIN_SAMPLES_PER_PATTERN = to_i("MIN_SAMPLES_PER_PATTERN", 120)
REGISTRY_PATH         = env("REGISTRY_PATH", "/var/data/registry.json")

if not TG_BOT_TOKEN:
    raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

app = FastAPI(title="guardiao-auto-bot (híbrido + ML opcional)", version="3.3.1")

# ========= Utils =========
def now_ts() -> int: return int(time.time())
def ts_str(ts=None) -> str:
    if ts is None: ts = now_ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ========= DB =========
def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=10000;")
    return con

def migrate_db():
    con = _connect(); cur = con.cursor()
    # timeline
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
    )""")
    # pending
    cur.execute("""CREATE TABLE IF NOT EXISTS pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER,
        suggested INTEGER,
        open INTEGER DEFAULT 1,
        seen TEXT,
        opened_at INTEGER,
        last_post_short TEXT,
        last_post_long  TEXT,
        last_conf_short REAL,
        last_conf_long  REAL,
        d_final INTEGER,
        base TEXT,
        pattern_key TEXT,
        closed_at INTEGER,
        outcome TEXT,
        stage TEXT
    )""")
    # score
    cur.execute("""CREATE TABLE IF NOT EXISTS score (
        id INTEGER PRIMARY KEY CHECK (id=1),
        green INTEGER DEFAULT 0,
        loss  INTEGER DEFAULT 0
    )""")
    if not cur.execute("SELECT 1 FROM score WHERE id=1").fetchone():
        cur.execute("INSERT INTO score (id, green, loss) VALUES (1,0,0)")

    # ml_log (para treino supervisionado)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ml_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL,
            pending_id INTEGER,
            after INTEGER,
            base TEXT,
            pattern_key TEXT,
            conf_short REAL,
            conf_long REAL,
            gap_short REAL,
            gap_long REAL,
            samples_short INTEGER,
            proba_json TEXT,
            chosen INTEGER,
            chosen_by TEXT,
            label INTEGER,
            stage TEXT,
            outcome TEXT
        )
    """)

    # defaults/correções
    try:
        cur.execute("UPDATE pending SET stage='OPEN' WHERE stage IS NULL OR stage=''")
        cur.execute("UPDATE pending SET outcome='PENDING' WHERE outcome IS NULL OR outcome=''")
    except sqlite3.OperationalError:
        pass
    con.commit(); con.close()

migrate_db()

def _exec_write(sql: str, params: tuple=()):
    for attempt in range(6):
        try:
            con = _connect(); cur = con.cursor()
            cur.execute(sql, params)
            con.commit(); con.close(); return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(0.25*(attempt+1)); continue
            raise

def _query_all(sql: str, params: tuple=()) -> List[sqlite3.Row]:
    con = _connect(); cur = con.cursor()
    rows = cur.execute(sql, params).fetchall()
    con.close()
    return rows

# ========= Telegram =========
async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse,
                                "disable_web_page_preview": True})

# ========= Score helpers =========
def bump_score(outcome: str):
    con = _connect(); cur = con.cursor()
    row = cur.execute("SELECT green, loss FROM score WHERE id=1").fetchone()
    g, l = (row["green"], row["loss"]) if row else (0, 0)
    if outcome.upper() == "GREEN": g += 1
    elif outcome.upper() == "LOSS": l += 1
    cur.execute("INSERT OR REPLACE INTO score (id, green, loss) VALUES (1,?,?)", (g, l))
    con.commit(); con.close()

def score_text() -> str:
    con = _connect()
    row = con.execute("SELECT green, loss FROM score WHERE id=1").fetchone()
    con.close()
    g, l = (int(row["green"]), int(row["loss"])) if row else (0, 0)
    total = g + l
    acc = (g/total*100.0) if total>0 else 0.0
    return f"{g} GREEN × {l} LOSS — {acc:.1f}%"

# ========= N-gram timeline =========
def append_timeline(seq: List[int]):
    for n in seq:
        _exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_tail(window:int) -> List[int]:
    con = _connect()
    rows = con.execute("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,)).fetchall()
    con.close()
    return [int(r["number"]) for r in rows][::-1]

def _ctx_counts(tail: List[int], ctx: List[int]) -> Dict[int,int]:
    k = len(ctx)
    cnt = {1:0,2:0,3:0,4:0}
    if k == 0 or len(tail) <= k: return cnt
    for i in range(k, len(tail)):
        if tail[i-k:i] == ctx:
            nxt = tail[i]
            if nxt in cnt: cnt[nxt] += 1
    return cnt

def _post_from_tail(tail: List[int], after: Optional[int], candidates: List[int]) -> Dict[int,float]:
    if not tail:
        return {c: 1.0/len(candidates) for c in candidates}
    # pesos por contexto (4,3,2,1)
    W = [0.46, 0.30, 0.16, 0.08]
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

    posts = {c: 0.0 for c in candidates}
    for lvl, ctx in [(4,ctx4),(3,ctx3),(2,ctx2),(1,ctx1)]:
        if not ctx: continue
        counts = _ctx_counts(tail, ctx[:-1])
        tot = sum(counts.values())
        if tot == 0: continue
        for n in candidates:
            posts[n] += [0.46, 0.30, 0.16, 0.08][4-lvl] * (counts.get(n,0)/tot)
    s = sum(posts.values()) or 1e-9
    return {k: v/s for k,v in posts.items()}

def _best_conf_gap(post: Dict[int,float]) -> Tuple[int,float,float]:
    top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)[:2]
    best = top[0][0]; conf = top[0][1]
    gap  = top[0][1] - (top[1][1] if len(top)>1 else 0.0)
    return best, conf, gap

# ========= Parsers =========
ENTRY_RX = re.compile(r"ENTRADA\s+CONFIRMADA", re.I)
SEQ_RX   = re.compile(r"Sequ[eê]ncia:\s*([^\n\r]+)", re.I)
AFTER_RX = re.compile(r"ap[oó]s\s+o\s+([1-4])", re.I)
KWOK_RX  = re.compile(r"\bKWOK\s*([1-4])\s*-\s*([1-4])", re.I)
SSH_RX   = re.compile(r"\bSS?H\s*([1-4])(?:-([1-4]))?(?:-([1-4]))?(?:-([1-4]))?", re.I)
ODD_RX   = re.compile(r"\bODD\b", re.I)
EVEN_RX  = re.compile(r"\bEVEN\b", re.I)
GREEN_RX = re.compile(r"(?:\bgr+e+e?n\b|\bwin\b|✅)", re.I)
LOSS_RX  = re.compile(r"(?:\blo+s+s?\b|\bred\b|❌|\bperdemos\b)", re.I)
PAREN_GROUP_RX = re.compile(r"\(([^)]*)\)")
ANY_14_RX      = re.compile(r"[1-4]")

# NOVO: captura direta "Número seco (G0): 2"
DIRECT_G0_RX   = re.compile(r"N[uú]mero\s*seco\s*\(?\s*G0\s*\)?\s*[:\-]\s*([1-4])", re.I)

def parse_candidates_and_pattern(t: str) -> Tuple[List[int], str]:
    m = KWOK_RX.search(t)
    if m:
        a,b = int(m.group(1)), int(m.group(2))
        base = sorted(list({a,b}))
        return base, f"KWOK-{a}-{b}"
    if ODD_RX.search(t):  return [1,3], "ODD"
    if EVEN_RX.search(t): return [2,4], "EVEN"
    m = SSH_RX.search(t)
    if m:
        nums = [int(g) for g in m.groups() if g]
        base = sorted(list(dict.fromkeys(nums)))[:4]
        return base, "SSH-" + "-".join(str(x) for x in base)
    m = SEQ_RX.search(t)
    if m:
        parts = [int(x) for x in re.findall(r"[1-4]", m.group(1))]
        seen, base = set(), []
        for n in parts:
            if n not in seen:
                seen.add(n); base.append(n)
            if len(base) == 3: break
        if base: return base, "SEQ"
    return [1,2,3,4], "GEN"

def parse_entry_text(text: str) -> Optional[Dict]:
    t = re.sub(r"\s+", " ", text).strip()

    # 1) PRIMEIRO: se já veio "Número seco (G0): X", disparamos direto
    mdir = DIRECT_G0_RX.search(t)
    if mdir:
        g0 = int(mdir.group(1))
        return {"seq": [], "after": None, "raw": t, "base": [1,2,3,4], "pattern_key": "DIRECT", "direct_g0": g0}

    # 2) Antigo: "ENTRADA CONFIRMADA"
    if not ENTRY_RX.search(t): return None
    base, pattern_key = parse_candidates_and_pattern(t)
    mseq = SEQ_RX.search(t)
    seq = [int(x) for x in re.findall(r"[1-4]", mseq.group(1))] if mseq else []
    mafter = AFTER_RX.search(t)
    after_num = int(mafter.group(1)) if mafter else None
    return {"seq": seq, "after": after_num, "raw": t, "base": base, "pattern_key": pattern_key}

def parse_close_numbers(text: str) -> List[int]:
    t = re.sub(r"\s+", " ", text)
    groups = PAREN_GROUP_RX.findall(t)
    if groups:
        nums = re.findall(r"[1-4]", groups[-1])
        return [int(x) for x in nums][:3]
    nums = ANY_14_RX.findall(t)
    return [int(x) for x in nums][:3]

# ========= Decisor (híbrido) =========
def choose_single_number_hybrid(after: Optional[int], candidates: List[int]) -> Tuple[Optional[int], float, float, int, Dict[int,float], Dict[int,float]]:
    candidates = sorted(list(dict.fromkeys([c for c in candidates if c in (1,2,3,4)]))) or [1,2,3,4]
    tail_s = get_tail(SHORT_WINDOW)
    tail_l = get_tail(LONG_WINDOW)
    post_s = _post_from_tail(tail_s, after, candidates)
    post_l = _post_from_tail(tail_l, after, candidates)
    b_s, c_s, g_s = _best_conf_gap(post_s)
    b_l, c_l, g_l = _best_conf_gap(post_l)
    best = None
    if b_s == b_l and c_s >= CONF_SHORT_MIN and c_l >= CONF_LONG_MIN and g_s >= GAP_MIN and g_l >= GAP_MIN:
        best = b_s
    return best, c_s, c_l, len(tail_s), post_s, post_l

# ========= ML =========
_MODELS: Dict[str,object] = {}

def _load_model_file(path: str):
    try:
        if os.path.exists(path):
            with open(path, "rb") as f:
                return pickle.load(f)
    except Exception as e:
        print(f"[ML] falha ao carregar {path}: {e}")
    return None

def load_models():
    pats = ["GEN","KWOK","SSH","SEQ","ODD","EVEN","GLOBAL"]
    for p in pats:
        fn = f"model_{p}.pkl" if p!="GLOBAL" else "model_GLOBAL.pkl"
        mdl = _load_model_file(os.path.join(MODEL_DIR, fn))
        if mdl is not None:
            _MODELS[p] = mdl
    if "GLOBAL" not in _MODELS:
        mdl = _load_model_file(MODEL_PATH)
        if mdl is not None:
            _MODELS["GLOBAL"] = mdl

def _top2(vals: Dict[int,float]) -> Tuple[float,float]:
    s = sorted(vals.values(), reverse=True)
    return (s[0], s[1]) if len(s)>1 else (s[0] if s else 0.0, 0.0)

def build_features(after: Optional[int], base: List[int], pattern_key: str,
                   conf_s: float, conf_l: float, post_s: Dict[int,float], post_l: Dict[int,float],
                   samples_s: int) -> Dict[str,float]:
    a_s,b_s = _top2(post_s); a_l,b_l = _top2(post_l)
    return {
        "after": float(after or 0), "has_after": 1.0 if after else 0.0,
        "conf_short": float(conf_s or 0.0), "conf_long": float(conf_l or 0.0),
        "gap_short": max(0.0, a_s-b_s), "gap_long": max(0.0, a_l-b_l),
        "samples_short": float(samples_s or 0), "base_len": float(len(base or [])),
        "pat_GEN": 1.0 if pattern_key.upper().startswith("GEN") else 0.0,
        "pat_KWOK":1.0 if pattern_key.upper().startswith("KWOK") else 0.0,
        "pat_SSH": 1.0 if pattern_key.upper().startswith("SSH") else 0.0,
        "pat_SEQ": 1.0 if pattern_key.upper().startswith("SEQ") else 0.0,
        "pat_ODD": 1.0 if pattern_key.upper()=="ODD" else 0.0,
        "pat_EVEN":1.0 if pattern_key.upper()=="EVEN" else 0.0,
    }

def ml_score(candidates: List[int],
             after: Optional[int], base: List[int], pattern_key: str,
             conf_s: float, conf_l: float, post_s: Dict[int,float], post_l: Dict[int,float],
             samples_s: int) -> Tuple[Optional[int], Dict[int,float], float]:
    if not _MODELS:
        return None, {}, 0.0
    feats_common = build_features(after, base, pattern_key, conf_s, conf_l, post_s, post_l, samples_s)
    X, order = [], []
    for c in candidates:
        x = dict(feats_common); x.update({"cand": float(c), "post_s": float(post_s.get(c,0.0)), "post_l": float(post_l.get(c,0.0))})
        X.append([x[k] for k in sorted(x.keys())]); order.append(c)
    mdl = _MODELS.get(pattern_key.split("-")[0].upper()) or _MODELS.get("GLOBAL")
    if mdl is None:
        return None, {}, 0.0
    try:
        proba = mdl.predict_proba(X)
        p_green = [float(p[1]) if len(p)>=2 else float(p[0]) for p in proba]
        by_cand = {order[i]: p_green[i] for i in range(len(order))}
        top = sorted(by_cand.items(), key=lambda kv: kv[1], reverse=True)[:2]
        if not top: return None, by_cand, 0.0
        best, p1 = top[0]; p2 = top[1][1] if len(top)>1 else 0.0
        gap = max(0.0, p1-p2)
        if p1 >= ML_MIN_PROBA and gap >= ML_GAP_MIN:
            return best, by_cand, gap
        return None, by_cand, gap
    except Exception as e:
        print(f"[ML] predict_proba erro: {e}")
        return None, {}, 0.0

# ========= Pending helpers =========
def get_open_pending() -> Optional[sqlite3.Row]:
    con = _connect()
    row = con.execute("SELECT * FROM pending WHERE open=1 ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return row

def _seen_list(row: sqlite3.Row) -> List[str]:
    return [s for s in (row["seen"] or "").strip().split("-") if s]

def _set_seen(row_id:int, seen_list:List[str]):
    _exec_write("UPDATE pending SET seen=? WHERE id=?", ("-".join(seen_list[:3]), row_id))

def _ensure_final_deadline_when_two(row: sqlite3.Row):
    if int(row["d_final"] or 0) > 0: return
    if len(_seen_list(row)) == 2:
        _exec_write("UPDATE pending SET d_final=? WHERE id=?", (now_ts() + FINAL_TIMEOUT, int(row["id"])))

def _close_now(row: sqlite3.Row, suggested:int, final_seen:List[str]):
    obs_nums = [int(x) for x in final_seen if x.isdigit()]
    if len(obs_nums) >= 1 and obs_nums[0] == suggested:
        outcome, stage_lbl = "GREEN", "G0"
    elif len(obs_nums) >= 2 and obs_nums[1] == suggested:
        outcome, stage_lbl = "GREEN", "G1"
    elif len(obs_nums) >= 3 and obs_nums[2] == suggested:
        outcome, stage_lbl = "GREEN", "G2"
    else:
        outcome, stage_lbl = "LOSS", "G2"

    _exec_write("""UPDATE pending
                      SET open=0, seen=?, closed_at=?, outcome=?, stage=?
                    WHERE id=?""",
                ("-".join(final_seen[:3]), now_ts(), outcome, stage_lbl, int(row["id"])))
    bump_score(outcome.upper())

    try:
        _exec_write("""
            UPDATE ml_log
               SET label = ?, stage = ?, outcome = ?
             WHERE pending_id = ?
        """, (1 if outcome.upper()=="GREEN" else 0, stage_lbl, outcome.upper(), int(row["id"])))
    except Exception as e:
        print(f"[ML] log label update fail: {e}")

    our_num_display = suggested if outcome=="GREEN" else "X"
    msg = (f"{'🟢' if outcome=='GREEN' else '🔴'} <b>{outcome}</b> — finalizado "
           f"(<b>{stage_lbl}</b>, nosso={our_num_display}, observados={'-'.join(final_seen[:3])}).\n"
           f"📊 Geral: {score_text()}")
    return msg

def open_pending(suggested: int, conf_short:float, conf_long:float,
                 post_short:Dict[int,float], post_long:Dict[int,float],
                 base:List[int], pattern_key:str):
    _exec_write("""INSERT INTO pending
        (created_at, suggested, open, seen, opened_at,
         last_post_short, last_post_long, last_conf_short, last_conf_long,
         d_final, base, pattern_key, closed_at, outcome, stage)
        VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,NULL,'PENDING','OPEN')
    """, (now_ts(), int(suggested), 1, "", now_ts(),
          json.dumps(post_short), json.dumps(post_long),
          float(conf_short), float(conf_long),
          json.dumps(base), pattern_key))

def _maybe_close_by_final_timeout():
    row = get_open_pending()
    if not row: return None
    d_final = int(row["d_final"] or 0)
    if d_final <= 0 or now_ts() < d_final: return None
    seen_list = _seen_list(row)
    if len(seen_list) == 2:
        seen_list.append("X")
        return _close_now(row, int(row["suggested"] or 0), seen_list)
    return None

# ========= Reporter & Reset =========
def _report_snapshot(last_secs:int=300) -> Dict[str,int]:
    since = now_ts() - max(60, int(last_secs))
    rows = _query_all("""
        SELECT outcome, stage FROM pending
        WHERE closed_at IS NOT NULL AND closed_at >= ?
    """, (since,))
    g0=g1=g2=0; l0=l1=l2=0
    for r in rows:
        oc = (r["outcome"] or "").upper()
        st = (r["stage"] or "").upper()
        if oc == "GREEN":
            if st == "G0": g0 += 1
            elif st == "G1": g1 += 1
            else: g2 += 1
        elif oc == "LOSS":
            if st == "G0": l0 += 1
            elif st == "G1": l1 += 1
            else: l2 += 1
    row = _query_all("SELECT green, loss FROM score WHERE id=1")
    row = row[0] if row else None
    g = int(row["green"] if row else 0); l = int(row["loss"] if row else 0)
    total = g + l
    acc = (g/total) if total>0 else 0.0
    return {"g0":g0,"g1":g1,"g2":g2,"l0":l0,"l1":l1,"l2":l2,"day_green":g,"day_loss":l,"day_acc":acc}

def _day_mood(acc: float) -> str:
    if acc >= GOOD_DAY_THRESHOLD: return "🔥 <b>Dia bom</b> — continuando bem!"
    if acc <= BAD_DAY_THRESHOLD:  return "⚠️ <b>Dia ruim</b> — atenção!"
    return "🔎 <b>Dia neutro</b> — operação regular."

async def _reporter_loop():
    while True:
        try:
            snap = _report_snapshot(REPORT_EVERY_SEC)
            gtot = snap["g0"] + snap["g1"] + snap["g2"]
            ltot = snap["l0"] + snap["l1"] + snap["l2"]
            txt = (
                "📈 <b>Relatório (últimos 5 min)</b>\n"
                f"G0: <b>{snap['g0']}</b> GREEN / <b>{snap['l0']}</b> LOSS\n"
                f"G1: <b>{snap['g1']}</b> GREEN / <b>{snap['l1']}</b> LOSS\n"
                f"G2: <b>{snap['g2']}</b> GREEN / <b>{snap['l2']}</b> LOSS\n"
                f"Total (5min): <b>{gtot}</b> GREEN × <b>{ltot}</b> LOSS\n"
                "—\n"
                f"📊 <b>Dia</b>: <b>{snap['day_green']}</b> GREEN × <b>{snap['day_loss']}</b> LOSS — "
                f"{snap['day_acc']*100:.1f}%\n"
                f"{_day_mood(snap['day_acc'])}"
            )
            await tg_send_text(TARGET_CHANNEL, txt)
        except Exception as e:
            print(f"[RELATORIO] erro: {e}")
        await asyncio.sleep(REPORT_EVERY_SEC)

async def _daily_reset_loop():
    try:
        tz = ZoneInfo(RESET_TZ)
    except Exception:
        tz = timezone.utc
    while True:
        try:
            now = datetime.now(tz)
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            await asyncio.sleep(max(1.0, (tomorrow - now).total_seconds()))
            _exec_write("INSERT OR REPLACE INTO score (id, green, loss) VALUES (1,0,0)")
            tzname = getattr(tz, "key", "UTC")
            await tg_send_text(TARGET_CHANNEL, f"🕛 <b>Reset diário (00:00 {tzname})</b>\n📊 Placar zerado para o novo dia.")
        except Exception as e:
            print(f"[RESET] erro: {e}")
            await asyncio.sleep(60)

# ========= Treino on-boot (opcional) =========
def _has_labels() -> bool:
    try:
        con = _connect(); cur = con.cursor()
        r = cur.execute("SELECT COUNT(1) FROM ml_log WHERE label IS NOT NULL").fetchone()
        con.close()
        return bool(r and int(r[0]) > 0)
    except Exception:
        return False

def _train_now():
    try:
        con = _connect(); con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT
                m.pending_id, m.after, m.base, m.pattern_key,
                m.conf_short, m.conf_long, m.gap_short, m.gap_long, m.samples_short,
                m.chosen, m.label, m.stage, m.outcome,
                p.last_post_short, p.last_post_long, p.suggested, p.outcome AS p_outcome, p.seen
        FROM ml_log m LEFT JOIN pending p ON p.id = m.pending_id
        WHERE m.label IS NOT NULL
        """).fetchall()
        con.close()
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier
        X, y = [], []
        for r in rows:
            try:
                base = json.loads(r["base"]) if r["base"] else []
                post_s = json.loads(r["last_post_short"]) if r["last_post_short"] else {}
                post_l = json.loads(r["last_post_long"]) if r["last_post_long"] else {}
            except Exception:
                base, post_s, post_l = [], {}, {}
            cand = int(r["chosen"]) if r["chosen"] is not None else None
            if cand is None: continue
            feats = build_features(int(r["after"] or 0) or None, base, (r["pattern_key"] or "GEN"),
                                   float(r["conf_short"] or 0.0), float(r["conf_long"] or 0.0),
                                   post_s, post_l, int(r["samples_short"] or 0))
            feats.update({"cand": float(cand), "post_s": float(post_s.get(cand,0.0)), "post_l": float(post_l.get(cand,0.0))})
            keys = sorted(feats.keys())
            X.append([feats[k] for k in keys]); y.append(int(r["label"]))
        if len(X) >= max(200, MIN_SAMPLES_PER_PATTERN) and len(set(y)) >= 2:
            clf = RandomForestClassifier(
                n_estimators=400, max_depth=None, min_samples_split=4, min_samples_leaf=2,
                class_weight="balanced_subsample", n_jobs=-1, random_state=42
            ).fit(X, y)
            os.makedirs(MODEL_DIR, exist_ok=True)
            import joblib; joblib.dump(clf, os.path.join(MODEL_DIR, "model_GLOBAL.pkl"))
            with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                json.dump({"models":{"GLOBAL":{"path":os.path.join(MODEL_DIR,"model_GLOBAL.pkl"),"samples":len(X)}}}, f, indent=2, ensure_ascii=False)
            print("[TRAIN] ✅ Treino GLOBAL salvo.")
        else:
            print("[TRAIN] ⚠️ Sem dados suficientes ainda para treino automático.")
    except Exception as e:
        print(f"[TRAIN] erro: {e}")

# ========= Rotas =========
@app.get("/")
async def root():
    return {"ok": True, "service": "guardiao-auto-bot (hybrid+ml)"}

@app.on_event("startup")
async def _boot_tasks():
    load_models()
    if TRAIN_ON_START and _has_labels():
        _train_now()
        load_models()
    try: asyncio.create_task(_reporter_loop())
    except Exception as e: print(f"[START] reporter error: {e}")
    try: asyncio.create_task(_daily_reset_loop())
    except Exception as e: print(f"[START] reset error: {e}")

@app.get("/health")
async def health():
    pend = get_open_pending()
    return {
        "ok": True,
        "db": DB_PATH,
        "pending_open": bool(pend),
        "pending_seen": (pend["seen"] if pend else ""),
        "d_final": int(pend["d_final"] or 0) if pend else 0,
        "time": ts_str()
    }

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    msg_timeout = _maybe_close_by_final_timeout()
    if msg_timeout: await tg_send_text(TARGET_CHANNEL, msg_timeout)

    data = await request.json()
    msg = data.get("channel_post") or data.get("message") \
        or data.get("edited_channel_post") or data.get("edited_message") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if SOURCE_CHANNEL and chat_id != str(SOURCE_CHANNEL):
        return {"ok": True, "skipped": "outro_chat"}
    if not text:
        return {"ok": True, "skipped": "sem_texto"}

    # Fechamentos
    if GREEN_RX.search(text) or LOSS_RX.search(text):
        pend = get_open_pending()
        nums = parse_close_numbers(text)
        if pend and nums:
            seen = _seen_list(pend)
            for n in nums:
                if len(seen) >= 3: break
                seen.append(str(int(n)))
                suggested = int(pend["suggested"] or 0)
                obs_nums = [int(x) for x in seen if x.isdigit()]
                if (len(obs_nums) >= 1 and obs_nums[0] == suggested) or \
                   (len(obs_nums) >= 2 and obs_nums[1] == suggested) or \
                   (len(obs_nums) >= 3 and obs_nums[2] == suggested):
                    out = _close_now(pend, suggested, seen)
                    await tg_send_text(TARGET_CHANNEL, out)
                    return {"ok": True, "closed": "green_imediato"}
            _set_seen(int(pend["id"]), seen)
            pend = get_open_pending()
            if pend and len(_seen_list(pend)) == 2:
                _ensure_final_deadline_when_two(pend)
            pend = get_open_pending()
            if pend and len(_seen_list(pend)) >= 3:
                out = _close_now(pend, int(pend["suggested"] or 0), _seen_list(pend))
                await tg_send_text(TARGET_CHANNEL, out)
                return {"ok": True, "closed": "loss_3_observados"}
        return {"ok": True, "noted_close": True}

    # ===== ENTRADA / DISPARO =====
    parsed = parse_entry_text(text)
    if not parsed:
        return {"ok": True, "skipped": "nao_eh_entrada_confirmada"}

    # Caso 1: veio já com "Número seco (G0): X" -> dispara direto
    if parsed.get("direct_g0"):
        chosen = int(parsed["direct_g0"])
        open_pending(chosen, 1.0, 1.0, {}, {}, [1,2,3,4], "DIRECT")
        await tg_send_text(
            TARGET_CHANNEL,
            f"🎯 <b>Número seco (G0):</b> <b>{chosen}</b>\n🤖 <b>Decisão:</b> DIRECT\n🔗 fonte: {SOURCE_CHANNEL}"
        )
        return {"ok": True, "posted": True, "best": chosen, "mode": "direct"}

    # Caso 2: texto antigo -> usar híbrido/ML
    msg_timeout = _maybe_close_by_final_timeout()
    if msg_timeout: await tg_send_text(TARGET_CHANNEL, msg_timeout)

    seq = parsed["seq"] or []
    if seq: append_timeline(seq)

    after       = parsed["after"]
    base        = parsed["base"] or [1,2,3,4]
    pattern_key = parsed["pattern_key"] or "GEN"

    # 1) híbrido
    best_h, conf_s, conf_l, samples_s, post_s, post_l = choose_single_number_hybrid(after, base)
    # 2) ML
    best_ml, proba_by_cand, ml_gap = ml_score(base, after, base, pattern_key, conf_s, conf_l, post_s, post_l, samples_s)

    # 3) ensemble
    chosen = None; chosen_by = None
    mode = ENSEMBLE_MODE.lower()
    if mode == "ml_only":
        if best_ml is not None: chosen, chosen_by = best_ml, "ml"
    elif mode == "blend":
        if (best_h is not None) and (best_ml is not None) and (best_h == best_ml):
            chosen, chosen_by = best_h, "ensemble"
        elif (best_h is None) and (best_ml is not None):
            chosen, chosen_by = best_ml, "ml"
        elif (best_h is not None) and (best_ml is None):
            chosen, chosen_by = best_h, "hybrid"
        elif (best_h is not None) and (best_ml is not None) and (best_h != best_ml):
            chosen, chosen_by = best_h, "hybrid"
    else:  # gate
        if best_h is not None:
            if best_ml is not None and best_ml != best_h:
                p_h = proba_by_cand.get(best_h, 0.0)
                if p_h >= ML_MIN_PROBA * 0.9:
                    chosen, chosen_by = best_h, "hybrid"
                else:
                    chosen, chosen_by = None, "blocked_by_ml"
            else:
                chosen, chosen_by = best_h, "hybrid"
        else:
            if best_ml is not None:
                chosen, chosen_by = best_ml, "ml"

    if chosen is None:
        return {"ok": True, "skipped_low_conf_or_disagree": True}

    # 4) abre pendência + log
    open_pending(chosen, conf_s, conf_l, post_s, post_l, base, pattern_key)
    try:
        gap_s_vals = sorted(post_s.values(), reverse=True)
        gap_l_vals = sorted(post_l.values(), reverse=True)
        gap_s = (gap_s_vals[0] - (gap_s_vals[1] if len(gap_s_vals)>1 else 0.0)) if gap_s_vals else 0.0
        gap_l = (gap_l_vals[0] - (gap_l_vals[1] if len(gap_l_vals)>1 else 0.0)) if gap_l_vals else 0.0
        _exec_write("""
            INSERT INTO ml_log (created_at, pending_id, after, base, pattern_key,
                                conf_short, conf_long, gap_short, gap_long, samples_short,
                                proba_json, chosen, chosen_by, label, stage, outcome)
            VALUES (?, (SELECT id FROM pending ORDER BY id DESC LIMIT 1), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
        """, (now_ts(),
              int(after or 0),
              json.dumps(base or []),
              str(pattern_key or "GEN"),
              float(conf_s), float(conf_l),
              float(max(0.0, gap_s)), float(max(0.0, gap_l)),
              int(samples_s or 0),
              json.dumps({int(k): float(v) for k,v in (proba_by_cand or {}).items()}),
              int(chosen), str(chosen_by or "hybrid")))
    except Exception as e:
        print(f"[ML] log insert fail: {e}")

    base_txt = ", ".join(str(x) for x in base) if base else "—"
    aft_txt  = f" após {after}" if after else ""
    txt = (
        f"🎯 <b>Número seco (G0):</b> <b>{chosen}</b>\n"
        f"🧩 <b>Padrão:</b> {pattern_key}{aft_txt}\n"
        f"🧮 <b>Base:</b> [{base_txt}]\n"
        f"📊 <b>Conf (curta/longa):</b> {conf_s*100:.2f}% / {conf_l*100:.2f}% | <b>Amostra≈</b>{samples_s}\n"
        f"🤖 <b>Decisão:</b> {chosen_by.upper() if chosen_by else 'HYBRID'}"
    )
    await tg_send_text(TARGET_CHANNEL, txt)
    return {"ok": True, "posted": True, "best": chosen}