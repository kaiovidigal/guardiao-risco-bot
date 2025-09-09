# -*- coding: utf-8 -*-
import os, re, json, time, sqlite3, shutil
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# ENV
# =========================
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()   # -100... ou @canal
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()

# **NOVO**: caminho do banco em disco persistente (Render)
DB_PATH = os.getenv("DB_PATH", "/data/data.db").strip()

if not TG_BOT_TOKEN or not PUBLIC_CHANNEL or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN, PUBLIC_CHANNEL e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# =========================
# Hiperparâmetros do modelo
# =========================
WINDOW = 400
DECAY  = 0.985
W4, W3, W2, W1 = 0.45, 0.30, 0.17, 0.08
ALPHA, BETA, GAMMA = 1.10, 0.65, 0.35

# sempre sugere (mesmo com confiança baixa)
MIN_CONF, MIN_SAMPLES, GAP_MIN = 0.0, 0, 0.0
COOLDOWN_S = 12

app = FastAPI(title="Fantan Guardião — Número Seco", version="2.3.1")

# =========================
# DB helpers (com WAL e migração)
# =========================
def ensure_db_dir():
    d = os.path.dirname(DB_PATH) or "."
    os.makedirs(d, exist_ok=True)

def migrate_old_db_if_needed():
    # caminho antigo durante build/exec do Render
    old = "/opt/render/project/src/data.db"
    if not os.path.exists(DB_PATH) and os.path.exists(old):
        try:
            shutil.copyfile(old, DB_PATH)
            print(f"📦 Migrado data.db -> {DB_PATH}")
        except Exception as e:
            print("⚠️ Falha ao migrar DB:", e)

def db() -> sqlite3.Connection:
    ensure_db_dir()
    con = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)  # autocommit
    con.row_factory = sqlite3.Row
    # PRAGMAs seguros para serviço
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    return con

def init_db():
    ensure_db_dir()
    migrate_old_db_if_needed()
    con = db()
    cur = con.cursor()

    # timeline
    cur.execute("""
    CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
    )""")

    # n-grams
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ngram_stats (
        n INTEGER NOT NULL,
        ctx TEXT NOT NULL,
        next INTEGER NOT NULL,
        weight REAL NOT NULL,
        PRIMARY KEY (n, ctx, next)
    )""")

    # stats por padrão
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stats_pattern (
        pattern_key TEXT NOT NULL,
        number INTEGER NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (pattern_key, number)
    )""")

    # stats por estratégia
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stats_strategy (
        strategy TEXT NOT NULL,
        number INTEGER NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (strategy, number)
    )""")

    # sugestões (dedupe)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS suggestions (
        source_msg_id INTEGER PRIMARY KEY,
        strategy TEXT,
        seq_raw TEXT,
        context_key TEXT,
        pattern_key TEXT,
        base TEXT,
        suggested_number INTEGER,
        stage TEXT,
        sent_at INTEGER
    )""")

    # ponte última sugestão por estratégia
    cur.execute("""
    CREATE TABLE IF NOT EXISTS last_by_strategy (
        strategy TEXT PRIMARY KEY,
        source_msg_id INTEGER,
        suggested_number INTEGER,
        context_key TEXT,
        pattern_key TEXT,
        stage TEXT,
        created_at INTEGER
    )""")

    # placar diário (geral)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0
    )""")

    # **NOVO** placar por estágio
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_score_stage (
        yyyymmdd TEXT NOT NULL,
        stage INTEGER NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (yyyymmdd, stage)
    )""")

    # cooldown
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cooldown (
        chat_id INTEGER PRIMARY KEY,
        last_ts REAL
    )""")

    # pendências (G0/G1/G2)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_outcome (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        strategy TEXT,
        suggested INTEGER NOT NULL,
        stage INTEGER NOT NULL,
        open INTEGER NOT NULL,
        window_left INTEGER NOT NULL
    )""")

    con.commit()
    con.close()

init_db()

def now_ts() -> int:
    return int(time.time())

def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")

# =========================
# Telegram
# =========================
async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    async with httpx.AsyncClient(timeout=15) as client:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True}
        r = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        if r.status_code != 200:
            print("❌ sendMessage failed:", r.text)

# =========================
# Timeline & n-grams
# =========================
def append_timeline(con: sqlite3.Connection, number: int):
    con.execute("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(number)))

def get_recent_tail(con: sqlite3.Connection, window: int = WINDOW) -> List[int]:
    rows = con.execute("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,)).fetchall()
    return [r["number"] for r in rows][::-1]

def update_ngrams(con: sqlite3.Connection, decay: float = DECAY, max_n: int = 5, window: int = WINDOW):
    tail = get_recent_tail(con, window)
    if len(tail) < 2:
        return
    for t in range(1, len(tail)):
        nxt = tail[t]
        dist = (len(tail)-1) - t
        w = (decay ** dist)
        for n in range(2, max_n+1):
            if t-(n-1) < 0: break
            ctx = tail[t-(n-1):t]
            ctx_key = ",".join(str(x) for x in ctx)
            con.execute("""
                INSERT INTO ngram_stats (n, ctx, next, weight)
                VALUES (?,?,?,?)
                ON CONFLICT(n, ctx, next) DO UPDATE SET weight = ngram_stats.weight + excluded.weight
            """, (n, ctx_key, int(nxt), float(w)))

def prob_from_ngrams(con: sqlite3.Connection, ctx: List[int], candidate: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    row = con.execute("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key)).fetchone()
    tot = (row["w"] or 0.0)
    if tot <= 0: return 0.0
    row2 = con.execute("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, candidate)).fetchone()
    w = (row2["weight"] or 0.0) if row2 else 0.0
    return w / tot

# =========================
# Parsers
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

def extract_strategy(text: str) -> Optional[str]:
    m = re.search(r"Estrat[eé]gia:\s*(\d+)", text, flags=re.I)
    return m.group(1) if m else None

def extract_seq_raw(text: str) -> Optional[str]:
    m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", text, flags=re.I)
    return m.group(1).strip() if m else None

def extract_after_num(text: str) -> Optional[int]:
    m = re.search(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", text, flags=re.I)
    return int(m.group(1)) if m else None

def bases_from_sequence_left_recent(seq_left_recent: List[int], k: int = 3) -> List[int]:
    seen, base = set(), []
    for n in seq_left_recent:
        if n not in seen:
            seen.add(n); base.append(n)
        if len(base) == k: break
    return base

def parse_bases_and_pattern(text: str) -> Tuple[List[int], str]:
    t = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"\bKWOK\s*([1-4])\s*-\s*([1-4])", t, flags=re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return [a,b], f"KWOK-{a}-{b}"
    if re.search(r"\bODD\b", t, flags=re.I):  return [1,3], "ODD"
    if re.search(r"\bEVEN\b", t, flags=re.I): return [2,4], "EVEN"
    m = re.search(r"\bSS?H\s*([1-4])(?:-([1-4]))?(?:-([1-4]))?(?:-([1-4]))?", t, flags=re.I)
    if m:
        nums = [int(g) for g in m.groups() if g]
        return nums, "SSH-" + "-".join(str(x) for x in nums)
    m = re.search(r"Sequ[eê]ncia:\s*([\d\s\|\-]+)", t, flags=re.I)
    if m:
        parts = re.findall(r"[1-4]", m.group(1))
        seq_left_recent = [int(x) for x in parts]
        base = bases_from_sequence_left_recent(seq_left_recent, 3)
        if base:
            return base, "SEQ"
    return [], "GEN"

GREEN_RE   = re.compile(r"APOSTA\s+ENCERRADA.*?GREEN.*?\((\d)\)", re.I | re.S)
RED_NUM_RE = re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\((.*?)\)", re.I | re.S)

def extract_green_number(text: str) -> Optional[int]:
    m = GREEN_RE.search(text)
    return int(m.group(1)) if m else None

def extract_red_last_left(text: str) -> Optional[int]:
    m = RED_NUM_RE.search(text)
    if not m: return None
    inside = m.group(1)
    nums = re.findall(r"[1-4]", inside)
    return int(nums[0]) if nums else None

def is_gale_info(text:str) -> Optional[int]:
    m = re.search(r"Estamos\s+no\s+(\d+)[ºo]?\s*gale", text, flags=re.I)
    return int(m.group(1)) if m else None

def is_analise(text:str) -> bool:
    return bool(re.search(r"\bANALISANDO\b", text, flags=re.I))

# =========================
# Stats
# =========================
def bump_pattern(con: sqlite3.Connection, pattern_key: str, number: int, won: bool):
    row = con.execute("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?",
                      (pattern_key, number)).fetchone()
    w = row["wins"] if row else 0
    l = row["losses"] if row else 0
    if won: w += 1
    else:   l += 1
    con.execute("""
      INSERT INTO stats_pattern (pattern_key, number, wins, losses)
      VALUES (?,?,?,?)
      ON CONFLICT(pattern_key, number)
      DO UPDATE SET wins=excluded.wins, losses=excluded.losses
    """, (pattern_key, number, w, l))

def bump_strategy(con: sqlite3.Connection, strategy: str, number: int, won: bool):
    row = con.execute("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?",
                      (strategy, number)).fetchone()
    w = row["wins"] if row else 0
    l = row["losses"] if row else 0
    if won: w += 1
    else:   l += 1
    con.execute("""
      INSERT INTO stats_strategy (strategy, number, wins, losses)
      VALUES (?,?,?,?)
      ON CONFLICT(strategy, number)
      DO UPDATE SET wins=excluded.wins, losses=excluded.losses
    """, (strategy, number, w, l))

def update_daily_score(con: sqlite3.Connection, won: bool) -> Tuple[int,int,float,int]:
    y = today_key()
    row = con.execute("SELECT wins, losses, streak FROM daily_score WHERE yyyymmdd=?", (y,)).fetchone()
    if not row:
        wins = 1 if won else 0
        losses = 0 if won else 1
        streak = 1 if won else 0
        con.execute("INSERT INTO daily_score (yyyymmdd,wins,losses,streak) VALUES (?,?,?,?)", (y, wins, losses, streak))
    else:
        wins, losses, streak = row["wins"], row["losses"], row["streak"]
        if won:
            wins += 1; streak += 1
        else:
            losses += 1; streak = 0
        con.execute("UPDATE daily_score SET wins=?, losses=?, streak=? WHERE yyyymmdd=?", (wins, losses, streak, y))
    total = wins + losses
    acc = (wins/total) if total else 0.0
    return wins, losses, acc, streak

def update_daily_score_stage(con: sqlite3.Connection, stage: int, won: bool):
    y = today_key()
    row = con.execute(
        "SELECT wins, losses FROM daily_score_stage WHERE yyyymmdd=? AND stage=?",
        (y, stage)
    ).fetchone()
    w = (row["wins"] if row else 0) + (1 if won else 0)
    l = (row["losses"] if row else 0) + (0 if won else 1)
    con.execute("""
      INSERT INTO daily_score_stage (yyyymmdd, stage, wins, losses)
      VALUES (?,?,?,?)
      ON CONFLICT(yyyymmdd, stage)
      DO UPDATE SET wins=excluded.wins, losses=excluded.losses
    """, (y, stage, w, l))

def read_stage_scores_today(con: sqlite3.Connection):
    y = today_key()
    rows = con.execute("""
      SELECT stage, wins, losses FROM daily_score_stage
      WHERE yyyymmdd=? ORDER BY stage
    """, (y,)).fetchall()
    out = {0:(0,0), 1:(0,0), 2:(0,0)}
    for r in rows:
        out[int(r["stage"])] = (int(r["wins"]), int(r["losses"]))
    return out

# =========================
# Dedup / Cooldown
# =========================
def already_suggested(con: sqlite3.Connection, source_msg_id: int) -> bool:
    row = con.execute("SELECT 1 FROM suggestions WHERE source_msg_id=?", (source_msg_id,)).fetchone()
    return row is not None

def remember_suggestion(con: sqlite3.Connection, source_msg_id:int, strategy:str, seq_raw:str,
                        context_key:str, pattern_key:str, base:List[int], suggested:int, stage:str):
    con.execute("""
      INSERT OR REPLACE INTO suggestions
      (source_msg_id, strategy, seq_raw, context_key, pattern_key, base, suggested_number, stage, sent_at)
      VALUES (?,?,?,?,?,?,?,?,?)
    """, (source_msg_id, strategy or "", seq_raw or "", context_key, pattern_key, json.dumps(base), suggested, stage, now_ts()))
    con.execute("""
      INSERT OR REPLACE INTO last_by_strategy
      (strategy, source_msg_id, suggested_number, context_key, pattern_key, stage, created_at)
      VALUES (?,?,?,?,?,?,?)
    """, (strategy or "", source_msg_id, suggested, context_key, pattern_key, stage, now_ts()))

def cooldown_ok(con: sqlite3.Connection, chat_id:int) -> bool:
    row = con.execute("SELECT last_ts FROM cooldown WHERE chat_id=?", (chat_id,)).fetchone()
    now = time.time()
    if row and now - row["last_ts"] < COOLDOWN_S:
        return False
    con.execute("INSERT OR REPLACE INTO cooldown (chat_id,last_ts) VALUES (?,?)", (chat_id, now))
    return True

# =========================
# Cálculo do número seco
# =========================
def ngram_backoff_score(con: sqlite3.Connection, tail: List[int], after_num: Optional[int], candidate: int) -> float:
    score = 0.0
    if not tail: return 0.0
    if after_num is not None:
        idxs = [i for i,v in enumerate(tail) if v == after_num]
        if not idxs:
            ctx4 = tail[-4:] if len(tail) >= 4 else []
            ctx3 = tail[-3:] if len(tail) >= 3 else []
            ctx2 = tail[-2:] if len(tail) >= 2 else []
            ctx1 = tail[-1:] if len(tail) >= 1 else []
        else:
            i = idxs[-1]
            ctx1 = tail[max(0, i): i+1]
            ctx2 = tail[max(0, i-1): i+1] if i-1 >= 0 else []
            ctx3 = tail[max(0, i-2): i+1] if i-2 >= 0 else []
            ctx4 = tail[max(0, i-3): i+1] if i-3 >= 0 else []
    else:
        ctx4 = tail[-4:] if len(tail) >= 4 else []
        ctx3 = tail[-3:] if len(tail) >= 3 else []
        ctx2 = tail[-2:] if len(tail) >= 2 else []
        ctx1 = tail[-1:] if len(tail) >= 1 else []
    parts = []
    if len(ctx4)==4: parts.append((W4, prob_from_ngrams(con, ctx4[:-1], candidate)))
    if len(ctx3)==3: parts.append((W3, prob_from_ngrams(con, ctx3[:-1], candidate)))
    if len(ctx2)==2: parts.append((W2, prob_from_ngrams(con, ctx2[:-1], candidate)))
    if len(ctx1)==1: parts.append((W1, prob_from_ngrams(con, ctx1[:-1], candidate)))
    for w,p in parts: score += w * p
    return score

def laplace_ratio(wins:int, losses:int) -> float:
    return (wins + 1.0) / (wins + losses + 2.0)

def confident_best(post: Dict[int,float], gap: float = 0.0) -> Optional[int]:
    a = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a: return None
    return a[0][0]

def suggest_number(con: sqlite3.Connection, base: List[int], pattern_key: str, strategy: Optional[str], after_num: Optional[int]) -> Tuple[Optional[int],float,int,Dict[int,float]]:
    if not base: base = [1,2,3,4]
    hour_block = int(datetime.now(timezone.utc).hour // 2)
    pat_key = f"{pattern_key}|h{hour_block}"

    tail = get_recent_tail(con, WINDOW)
    scores: Dict[int, float] = {}

    for c in base:
        ng = ngram_backoff_score(con, tail, after_num, c)
        rowp = con.execute("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pat_key, c)).fetchone()
        pw = rowp["wins"] if rowp else 0
        pl = rowp["losses"] if rowp else 0
        p_pat = laplace_ratio(pw, pl)
        p_str = 1/len(base)
        if strategy:
            rows = con.execute("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?", (strategy, c)).fetchone()
            sw = rows["wins"] if rows else 0
            sl = rows["losses"] if rows else 0
            p_str = laplace_ratio(sw, sl)
        prior = 1.0/len(base)
        scores[c] = (prior) * ((ng or 1e-6) ** ALPHA) * (p_pat ** BETA) * (p_str ** GAMMA)

    total = sum(scores.values()) or 1e-9
    post = {k: v/total for k,v in scores.items()}

    number = confident_best(post)
    conf = post.get(number, 0.0) if number is not None else 0.0

    roww = con.execute("SELECT SUM(weight) AS s FROM ngram_stats").fetchone()
    samples = int(roww["s"] or 0)
    return number, conf, samples, post

# =========================
# Mensagens
# =========================
def build_suggestion_msg(number:int, base:List[int], pattern_key:str, after_num:Optional[int], conf:float, samples:int, stage:str="G0") -> str:
    base_txt = ", ".join(str(x) for x in base) if base else "—"
    aft_txt = f" após {after_num}" if after_num else ""
    header = "⚠️ Confiança baixa\n" if conf < 0.55 else ""
    return header + (
        f"🎯 <b>Número seco ({stage}):</b> <b>{number}</b>\n"
        f"🧩 <b>Padrão:</b> {pattern_key}{aft_txt}\n"
        f"🧮 <b>Base:</b> [{base_txt}]\n"
        f"📊 Conf: {conf*100:.2f}% | Amostra≈{samples}"
    )

async def send_scoreboard(con: sqlite3.Connection, w:int,l:int,acc:float,int_streak:int):
    s = read_stage_scores_today(con)
    g0w,g0l = s[0]; g1w,g1l = s[1]; g2w,g2l = s[2]
    txt = (
        f"📊 <b>Placar do dia</b> — 🟢 {w} 🔴 {l}\n"
        f"✅ Acerto: {acc*100:.2f}%\n"
        f"🔥 Streak: {int_streak} GREEN(s)\n\n"
        f"📍 <b>Por estágio</b>:\n"
        f"• G0: 🟢 {g0w} 🔴 {g0l}\n"
        f"• G1: 🟢 {g1w} 🔴 {g1l}\n"
        f"• G2: 🟢 {g2w} 🔴 {g2l}"
    )
    await tg_send_text(PUBLIC_CHANNEL, txt)

# =========================
# Webhook models
# =========================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

# =========================
# Pending outcome helpers
# =========================
def open_pending(con: sqlite3.Connection, strategy: Optional[str], suggested: int):
    con.execute(
        "INSERT INTO pending_outcome (created_at,strategy,suggested,stage,open,window_left) VALUES (?,?,?,?,1,3)",
        (now_ts(), strategy or "", int(suggested), 0)
    )

def close_pending_with_result(con: sqlite3.Connection, n_real: int) -> List[Tuple[int,int,float,int]]:
    scores_to_publish: List[Tuple[int,int,float,int]] = []
    rows = con.execute(
        "SELECT id, strategy, suggested, stage, window_left FROM pending_outcome WHERE open=1 ORDER BY id"
    ).fetchall()
    for r in rows:
        pid, strat, sug, stage, left = r["id"], (r["strategy"] or ""), int(r["suggested"]), int(r["stage"]), int(r["window_left"])
        if n_real == sug:
            stage_hit = 3 - left  # 3->G0, 2->G1, 1->G2
            bump_pattern(con, "PEND", sug, True)
            if strat:
                bump_strategy(con, strat, sug, True)
            update_daily_score_stage(con, stage_hit, True)
            w,l,acc,streak = update_daily_score(con, True)
            con.execute("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
            scores_to_publish.append((w,l,acc,streak))
        else:
            left -= 1
            if left <= 0:
                bump_pattern(con, "PEND", sug, False)
                if strat:
                    bump_strategy(con, strat, sug, False)
                update_daily_score_stage(con, 2, False)  # loss até G2
                w,l,acc,streak = update_daily_score(con, False)
                con.execute("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
                scores_to_publish.append((w,l,acc,streak))
            else:
                con.execute("UPDATE pending_outcome SET window_left=? WHERE id=?", (left, pid))
    return scores_to_publish

# =========================
# Routes
# =========================
@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>"}

@app.get("/debug/score")
async def debug_score():
    con = db()
    out = [dict(r) for r in con.execute("SELECT * FROM daily_score ORDER BY yyyymmdd DESC LIMIT 14").fetchall()]
    con.close()
    return out

@app.get("/debug/last")
async def debug_last():
    con = db()
    tail = get_recent_tail(con, 30)
    con.close()
    return {"tail_old_to_new": tail}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg:
        return {"ok": True}

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    source_msg_id = msg.get("message_id")
    text = msg.get("text") or msg.get("caption") or ""
    t = re.sub(r"\s+", " ", text).strip()

    con = db()

    # 1) GREEN / RED / GALE → timeline + fechar pendências
    try:
        gnum = extract_green_number(t)
        gale_n = is_gale_info(t)
        red_last = extract_red_last_left(t)

        n_observed = None
        if gnum is not None:
            append_timeline(con, gnum); update_ngrams(con); n_observed = gnum
        elif red_last is not None:
            append_timeline(con, red_last); update_ngrams(con); n_observed = red_last

        published_any = False
        if n_observed is not None:
            scores_list = close_pending_with_result(con, n_observed)
            con.commit()
            for (w,l,acc,streak) in scores_list:
                await send_scoreboard(con, w,l,acc,streak)
                published_any = True

        # aprendizado direto por estratégia quando GREEN
        strat = extract_strategy(t) or ""
        row = con.execute("SELECT suggested_number, pattern_key FROM last_by_strategy WHERE strategy=?", (strat,)).fetchone()
        if row and gnum is not None:
            suggested = int(row["suggested_number"])
            pat_key   = row["pattern_key"] or "GEN"
            won = (suggested == int(gnum))
            bump_pattern(con, pat_key, suggested, won)
            if strat:
                bump_strategy(con, strat, suggested, won)
            w,l,acc,streak = update_daily_score(con, won)
            await send_scoreboard(con, w,l,acc,streak)
            published_any = True

        con.commit(); con.close()
        if gnum is not None or red_last is not None or gale_n is not None:
            return {"ok": True, "learned_or_updated": True, "published_score": published_any}
    finally:
        pass

    # 2) ANALISANDO → alimentar timeline
    if is_analise(t):
        seq_raw = extract_seq_raw(t)
        if seq_raw:
            parts = re.findall(r"[1-4]", seq_raw)
            seq_left_recent = [int(x) for x in parts]
            seq_old_to_new  = seq_left_recent[::-1]
            for n in seq_old_to_new:
                append_timeline(con, n)
                for (w,l,acc,streak) in close_pending_with_result(con, n):
                    await send_scoreboard(con, w,l,acc,streak)
            update_ngrams(con)
            con.commit()
        con.close()
        return {"ok": True, "analise": True}

    # 3) ENTRADA CONFIRMADA → sugerir 1 número seco
    if not is_real_entry(t):
        con.close()
        return {"ok": True, "skipped": True}

    if already_suggested(con, source_msg_id):
        con.close()
        return {"ok": True, "dup": True}

    strategy   = extract_strategy(t) or ""
    seq_raw    = extract_seq_raw(t) or ""
    after_num  = extract_after_num(t)
    base, pattern_key = parse_bases_and_pattern(t)
    if not base:
        base = [1,2,3,4]; pattern_key = "GEN"

    number, conf, samples, post = suggest_number(con, base, pattern_key, strategy, after_num)

    remember_suggestion(con, source_msg_id, strategy, seq_raw, "CTX", pattern_key, base, number, stage="G0")
    open_pending(con, strategy, number)
    con.commit(); con.close()

    out = build_suggestion_msg(number, base, pattern_key, after_num, conf, samples, stage="G0")
    await tg_send_text(PUBLIC_CHANNEL, out)
    return {"ok": True, "sent": True, "conf": conf, "samples": samples}