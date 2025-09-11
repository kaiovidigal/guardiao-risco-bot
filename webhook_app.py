# -*- coding: utf-8 -*-
# Fan Tan — Guardião (SINAL DO CANAL apenas) — Coletor/Replicador
# - NÃO possui IA autônoma. Não dispara sinais sozinho.
# - Processa mensagens do canal: ANALISANDO / ENTRADA CONFIRMADA / GREEN / RED
# - Treina n-grams com as sequências, sugere G0 no momento da ENTRADA (sinal do canal)
# - Recuperação G1/G2 com mensagens imediatas:
#     "✅ GREEN (recuperação G1)" / "✅ GREEN (recuperação G2)"
# - Placar consolidado a cada 30 minutos (G0, G1, G2 e Loss)
#
# Rotas:
#   POST /webhook/<WEBHOOK_TOKEN>   (Telegram webhook)
#   GET  /                          (ping)
#   GET  /debug/state, /debug/reason, /debug/samples, /debug/flush
#
# ENV obrigatórias: TG_BOT_TOKEN, WEBHOOK_TOKEN
# ENV opcionais:   PUBLIC_CHANNEL (chat id), DB_PATH (/data/data.db)
#
# Start command (Render/Procfile):
#   web: gunicorn webhook_app_collector:app --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT

import os, re, json, time, sqlite3, asyncio, shutil
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime, timezone, timedelta
from collections import Counter

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================================
# ENV / CONFIG
# =========================================
DB_PATH       = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
TG_BOT_TOKEN  = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL= os.getenv("PUBLIC_CHANNEL", "").strip()   # canal réplica (ex: -100...)
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
FLUSH_KEY     = os.getenv("FLUSH_KEY", "meusegredo123").strip()

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN e WEBHOOK_TOKEN.")

TELEGRAM_API  = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
REPL_ENABLED  = True
REPL_CHANNEL  = PUBLIC_CHANNEL

# Sinal do canal (parâmetros enxutos e estáveis)
WINDOW = 400
DECAY  = 0.985
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12
ALPHA, BETA, GAMMA = 1.05, 0.70, 0.40
GAP_MIN = 0.08
MIN_SAMPLES = 1000

# Limiares de qualidade do G0 (fixos)
MIN_CONF_G0 = 0.55
MIN_GAP_G0  = 0.040

MAX_STAGE = 3   # G0,G1,G2

app = FastAPI(title="Fantan Guardião — Coletor (sinal do canal)", version="1.0.0")

# =========================================
# DB helpers (SQLite + migração)
# =========================================
OLD_DB_CANDIDATES = [
    "/var/data/data.db",
    "/opt/render/project/src/data.db",
    "/opt/render/project/src/data/data.db",
    "/data/data.db",
]
def _ensure_db_dir():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except Exception as e:
        print(f"[DB] mkdir: {e}")

def _migrate_old_db_if_needed():
    if os.path.exists(DB_PATH):
        return
    for src in OLD_DB_CANDIDATES:
        if os.path.exists(src):
            try:
                _ensure_db_dir()
                shutil.copy2(src, DB_PATH)
                print(f"[DB] Migrado {src} -> {DB_PATH}")
                return
            except Exception as e:
                print(f"[DB] Migração falhou {src}: {e}")

_ensure_db_dir()
_migrate_old_db_if_needed()

def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def exec_write(sql: str, params: tuple = (), retries: int = 8, wait: float = 0.25):
    for _ in range(retries):
        try:
            con = _connect(); con.execute(sql, params); con.commit(); con.close(); return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(wait); continue
            raise
    raise sqlite3.OperationalError("Banco bloqueado após várias tentativas.")

def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    con = _connect(); rows = con.execute(sql, params).fetchall(); con.close(); return rows

def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    con = _connect(); row = con.execute(sql, params).fetchone(); con.close(); return row

def init_db():
    con = _connect(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, number INTEGER NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram_stats (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, next INTEGER NOT NULL, weight REAL NOT NULL,
        PRIMARY KEY (n, ctx, next))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS stats_pattern (
        pattern_key TEXT NOT NULL, number INTEGER NOT NULL, wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (pattern_key, number))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS stats_strategy (
        strategy TEXT NOT NULL, number INTEGER NOT NULL, wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (strategy, number))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS suggestions (
        source_msg_id INTEGER PRIMARY KEY, strategy TEXT, seq_raw TEXT, context_key TEXT, pattern_key TEXT, base TEXT,
        suggested_number INTEGER, stage TEXT, sent_at INTEGER)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS last_by_strategy (
        strategy TEXT PRIMARY KEY, source_msg_id INTEGER, suggested_number INTEGER,
        context_key TEXT, pattern_key TEXT, stage TEXT, created_at INTEGER)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY, g0 INTEGER NOT NULL DEFAULT 0, g1 INTEGER NOT NULL DEFAULT 0,
        g2 INTEGER NOT NULL DEFAULT 0, loss INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_outcome (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, strategy TEXT, suggested INTEGER NOT NULL,
        stage INTEGER NOT NULL, open INTEGER NOT NULL, window_left INTEGER NOT NULL, seen_numbers TEXT DEFAULT '',
        announced INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'CHAN')""")
    con.commit()
    for alter in [
        "ALTER TABLE pending_outcome ADD COLUMN seen_numbers TEXT DEFAULT ''",
        "ALTER TABLE pending_outcome ADD COLUMN announced INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE pending_outcome ADD COLUMN source TEXT NOT NULL DEFAULT 'CHAN'",
    ]:
        try: cur.execute(alter); con.commit()
        except sqlite3.OperationalError: pass
    con.close()

init_db()

# =========================================
# Utils / Telegram
# =========================================
def now_ts() -> int: return int(time.time())
def utc_iso() -> str: return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
def today_key_local() -> str: return datetime.now(timezone.utc).strftime("%Y%m%d")

async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True})

async def tg_broadcast(text: str, parse: str="HTML"):
    if REPL_ENABLED and REPL_CHANNEL: await tg_send_text(REPL_CHANNEL, text, parse)

async def send_green(stage_txt:str="G0", num:int=0):
    if stage_txt == "G0":
        await tg_broadcast(f"✅ <b>GREEN</b> — Número: <b>{num}</b> (G0)")
    elif stage_txt == "G1":
        await tg_broadcast(f"✅ <b>GREEN (recuperação G1)</b> — Número: <b>{num}</b>")
    else:
        await tg_broadcast(f"✅ <b>GREEN (recuperação G2)</b> — Número: <b>{num}</b>")

async def send_loss(num:int=0):
    await tg_broadcast(f"❌ <b>LOSS</b> — Número: <b>{num}</b> (G0)")

# =========================================
# Timeline & n-grams
# =========================================
def append_timeline(n: int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_recent_tail(window: int = WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def update_ngrams(decay: float = DECAY, max_n: int = 5, window: int = WINDOW):
    tail = get_recent_tail(window)
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
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    tot_row = query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key))
    tot = (tot_row["w"] or 0.0) if tot_row else 0.0
    if tot <= 0: return 0.0
    w_row = query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, candidate))
    w = (w_row["weight"] or 0.0) if w_row else 0.0
    return w / tot

# =========================================
# Parsers (canal)
# =========================================
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
    if m: a,b = int(m.group(1)), int(m.group(2)); return [a,b], f"KWOK-{a}-{b}"
    if re.search(r"\bODD\b", t, flags=re.I):  return [1,3], "ODD"
    if re.search(r"\bEVEN\b", t, flags=re.I): return [2,4], "EVEN"
    m = re.search(r"\bSS?H\s*([1-4])(?:-([1-4]))?(?:-([1-4]))?(?:-([1-4]))?", t, flags=re.I)
    if m:
        nums = [int(g) for g in m.groups() if g]
        return nums, "SSH-" + "-".join(str(x) for x in nums)
    m = re.search(r"Sequ[eê]ncia:\s*([\d\s\|\-]+)", t, flags=re.I)
    if m:
        parts = re.findall(r"[1-4]", m.group(1))
        base = bases_from_sequence_left_recent([int(x) for x in parts], 3)
        if base: return base, "SEQ"
    return [], "GEN"

# GREEN/RED — captura o ÚLTIMO número entre parênteses
GREEN_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bGREEN\b.*?\((.*?)\)", re.I | re.S),
    re.compile(r"\bGREEN\b.*?\((.*?)\)", re.I | re.S),
    re.compile(r"\bGREEN\b.*?Número[:\s]*([1-4])", re.I | re.S),
]
RED_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\((.*?)\)", re.I | re.S),
    re.compile(r"\bRED\b.*?\((.*?)\)", re.I | re.S),
    re.compile(r"\bLOSS\b.*?Número[:\s]*([1-4])", re.I | re.S),
]

def _last_num_in_group(g: str) -> Optional[int]:
    if not g: return None
    nums = re.findall(r"[1-4]", g)
    return int(nums[-1]) if nums else None

def extract_green_number(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in GREEN_PATTERNS:
        m = rx.search(t)
        if m:
            g = m.group(1) if m.lastindex else None
            return _last_num_in_group(g) if g is not None else _last_num_in_group(m.group(0))
    return None

def extract_red_last_left(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in RED_PATTERNS:
        m = rx.search(t)
        if m:
            g = m.group(1) if m.lastindex else None
            return _last_num_in_group(g) if g is not None else _last_num_in_group(m.group(0))
    return None

def is_analise(text:str) -> bool:
    return bool(re.search(r"\bANALISANDO\b", text, flags=re.I))

# =========================================
# Placar e recuperação
# =========================================
def update_daily_score(stage: int, won: bool):
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0,g1,g2,loss,streak = (row["g0"],row["g1"],row["g2"],row["loss"],row["streak"]) if row else (0,0,0,0,0)
    if won:
        if stage == 0: g0 += 1
        elif stage == 1: g1 += 1
        else: g2 += 1
        streak += 1
    else:
        loss += 1; streak = 0
    exec_write("""INSERT INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
                  VALUES (?,?,?,?,?,?)
                  ON CONFLICT(yyyymmdd) DO UPDATE SET
                    g0=excluded.g0, g1=excluded.g1, g2=excluded.g2, loss=excluded.loss, streak=excluded.streak""",
               (y,g0,g1,g2,loss,streak))
    total = g0 + g1 + g2 + loss
    acc = ((g0+g1+g2)/total) if total else 0.0
    return g0,g1,g2,loss,acc,streak

async def send_scoreboard_30m():
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0 = row["g0"] if row else 0
    g1 = row["g1"] if row else 0
    g2 = row["g2"] if row else 0
    loss = row["loss"] if row else 0
    streak = row["streak"] if row else 0
    total = g0 + g1 + g2 + loss
    acc = ((g0+g1+g2)/total*100.0) if total else 0.0
    txt = (f"📊 <b>Placar (30m)</b>\n"
           f"🟢 G0:{g0} • G1:{g1} • G2:{g2} • 🔴 Loss:{loss}\n"
           f"✅ {acc:.2f}% • 🔥 {streak}")
    await tg_broadcast(txt)

async def _score_task():
    while True:
        try:
            await send_scoreboard_30m()
        except Exception as e:
            print(f"[SCORE30] erro: {e}")
        await asyncio.sleep(30*60)

# Pendências
def open_pending(strategy: Optional[str], suggested: int, source: str = "CHAN"):
    exec_write("""INSERT INTO pending_outcome
                  (created_at,strategy,suggested,stage,open,window_left,seen_numbers,announced,source)
                  VALUES (?,?,?,?,1,?, '', 1, ?)""", (now_ts(), strategy or "", int(suggested), 0, MAX_STAGE, source))

async def close_pending_with_result(n_observed: int):
    rows = query_all("""SELECT id, strategy, suggested, stage, window_left, seen_numbers, source
                        FROM pending_outcome WHERE open=1 ORDER BY id ASC""")
    if not rows: return
    for r in rows:
        pid, suggested, stage, left = r["id"], int(r["suggested"]), int(r["stage"]), int(r["window_left"])
        seen_new = ((r["seen_numbers"] or "") + ("," if (r["seen_numbers"] or "") else "") + str(int(n_observed))).strip()
        if int(n_observed) == suggested:
            # GREEN
            exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?", (seen_new, pid))
            if stage == 0:
                update_daily_score(0, True)
                await send_green("G0", suggested)
            elif stage == 1:
                update_daily_score(1, True)
                await send_green("G1", suggested)
            else:
                update_daily_score(2, True)
                await send_green("G2", suggested)
        else:
            # ainda não bateu
            if left > 1:
                exec_write("""UPDATE pending_outcome SET stage=stage+1, window_left=window_left-1, seen_numbers=? WHERE id=?""",
                           (seen_new, pid))
                if stage == 0:
                    # Primeiro erro conta LOSS e avisa
                    update_daily_score(0, False)
                    await send_loss(suggested)
            else:
                exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?", (seen_new, pid))

# =========================================
# Heurística leve do canal (G0 no momento da ENTRADA)
# =========================================
def tail_top2_boost(tail: List[int], k:int=40) -> Dict[int, float]:
    boosts={1:1.00,2:1.00,3:1.00,4:1.00}
    if not tail: return boosts
    tail_k = tail[-k:] if len(tail)>=k else tail[:]
    c=Counter(tail_k); freq=c.most_common()
    if len(freq)>=1: boosts[freq[0][0]]=1.04
    if len(freq)>=2: boosts[freq[1][0]]=1.02
    return boosts

def ngram_backoff_score(tail: List[int], after_num: Optional[int], candidate: int) -> float:
    if not tail: return 0.0
    # contexto padrão (ignora after_num para simplificar sinal do canal)
    ctx4=tail[-4:] if len(tail)>=4 else []; ctx3=tail[-3:] if len(tail)>=3 else []
    ctx2=tail[-2:] if len(tail)>=2 else [];  ctx1=tail[-1:] if len(tail)>=1 else []
    parts=[]
    if len(ctx4)==4: parts.append((W4, prob_from_ngrams(ctx4[:-1], candidate)))
    if len(ctx3)==3: parts.append((W3, prob_from_ngrams(ctx3[:-1], candidate)))
    if len(ctx2)==2: parts.append((W2, prob_from_ngrams(ctx2[:-1], candidate)))
    if len(ctx1)==1: parts.append((W1, prob_from_ngrams(ctx1[:-1], candidate)))
    return sum(w*p for w,p in parts)

def _confident_best(post: Dict[int,float], gap_min: float = GAP_MIN) -> Tuple[Optional[int], float, float]:
    a=sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a: return None, 0.0, 0.0
    if len(a)==1: return a[0][0], a[0][1], a[0][1]
    top1, top2 = a[0], a[1]
    gap = top1[1]-top2[1]
    return (top1[0], top1[1], gap) if gap >= gap_min else (None, top1[1], gap)

def suggest_number_for_channel(base: List[int], pattern_key: str, after_num: Optional[int]) -> Tuple[Optional[int], float, int, float]:
    if not base: base=[1,2,3,4]
    tail = get_recent_tail(WINDOW)
    boosts = tail_top2_boost(tail, k=40)
    scores: Dict[int,float] = {}
    for c in base:
        ng = ngram_backoff_score(tail, after_num, c)
        # Laplace leve por pattern_key/hora
        hour_block=int(datetime.now(timezone.utc).hour // 2)
        pat_key=f"{pattern_key}|h{hour_block}"
        rowp = query_one("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pat_key, c))
        pw,pl = (rowp["wins"] if rowp else 0),(rowp["losses"] if rowp else 0)
        p_pat = (pw + 1.0) / (pw + pl + 2.0)
        boost_pat = 1.05 if p_pat>=0.60 else (0.98 if p_pat<=0.40 else 1.00)
        prior = 1.0/len(base)
        scores[c] = prior * ((ng or 1e-6) ** ALPHA) * (p_pat ** BETA) * boost_pat * boosts.get(c,1.0)
    total = sum(scores.values()) or 1e-9
    post = {k:v/total for k,v in scores.items()}
    num, conf, gap = _confident_best(post, gap_min=MIN_GAP_G0)
    samples_row = query_one("SELECT SUM(weight) AS s FROM ngram_stats"); samples = int((samples_row["s"] or 0) if samples_row else 0)
    # filtros
    if samples < MIN_SAMPLES: return None, 0.0, samples, gap
    if num is None: return None, 0.0, samples, gap
    if conf < MIN_CONF_G0 or gap < MIN_GAP_G0: return None, conf, samples, gap
    return int(num), float(conf), int(samples), float(gap)

def build_suggestion_msg(number:int, base:List[int], pattern_key:str, after_num:Optional[int], conf:float, samples:int) -> str:
    base_txt = ", ".join(str(x) for x in base) if base else "—"
    aft_txt = f" após {after_num}" if after_num else ""
    return (f"🎯 <b>Número seco (G0):</b> <b>{number}</b>\n"
            f"🧩 <b>Padrão:</b> {pattern_key}{aft_txt}\n"
            f"🧮 <b>Base:</b> [{base_txt}]\n"
            f"📊 Conf: {conf*100:.2f}% | Amostra≈{samples}")

# =========================================
# Relatórios / Saúde
# =========================================
def _get_scalar(sql: str, params: tuple = (), default: int|float = 0):
    row = query_one(sql, params)
    if not row: return default
    try: return row[0] if row[0] is not None else default
    except: keys=row.keys(); return row[keys[0]] if keys and row[keys[0]] is not None else default

def _fmt_bytes(n: int) -> str:
    try: n=float(n)
    except: return "—"
    for u in ["B","KB","MB","GB","TB","PB"]:
        if n<1024.0: return f"{n:.1f} {u}"
        n/=1024.0
    return f"{n:.1f} EB"

def _health_text() -> str:
    timeline_cnt=_get_scalar("SELECT COUNT(*) FROM timeline")
    ngram_rows=_get_scalar("SELECT COUNT(*) FROM ngram_stats")
    ngram_samples=_get_scalar("SELECT SUM(weight) FROM ngram_stats")
    pend_open=_get_scalar("SELECT COUNT(*) FROM pending_outcome WHERE open=1")
    y=today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0=row["g0"] if row else 0; g1=row["g1"] if row else 0; g2=row["g2"] if row else 0
    loss=row["loss"] if row else 0; streak=row["streak"] if row else 0
    total = g0+g1+g2+loss; acc = ((g0+g1+g2)/total*100.0) if total else 0.0
    return ("🩺 <b>Saúde do Guardião</b>\n"
            f"⏱️ UTC: <code>{utc_iso()}</code>\n—\n"
            f"🗄️ timeline: <b>{timeline_cnt}</b>\n"
            f"📚 ngram_stats: <b>{ngram_rows}</b> | amostras≈<b>{int(ngram_samples or 0)}</b>\n"
            f"⏳ pendências abertas: <b>{int(pend_open)}</b>\n—\n"
            f"📊 G0:{g0} • G1:{g1} • G2:{g2} • Loss:{loss} | ✅ {acc:.2f}% • 🔥 {streak}\n")

async def _health_reporter_task():
    while True:
        try: await tg_broadcast(_health_text())
        except Exception as e: print(f"[HEALTH] erro: {e}")
        await asyncio.sleep(30*60)

# =========================================
# Webhook models & rotas
# =========================================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>"}

@app.on_event("startup")
async def _boot_tasks():
    asyncio.create_task(_health_reporter_task())
    asyncio.create_task(_score_task())

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg: return {"ok": True}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    t = re.sub(r"\s+", " ", text)

    # 1) GREEN / RED — fecha/atualiza pendências e alimenta timeline
    gnum = extract_green_number(t)
    redn = extract_red_last_left(t)
    if gnum is not None or redn is not None:
        n_observed = gnum if gnum is not None else redn
        append_timeline(n_observed); update_ngrams()
        await close_pending_with_result(n_observed)
        return {"ok": True, "observed": n_observed}

    # 2) ANALISANDO — registra sequência (apenas treino), sem sinal
    if is_analise(t):
        seq_raw = extract_seq_raw(t)
        if seq_raw:
            parts = re.findall(r"[1-4]", seq_raw)
            seq_left_recent = [int(x) for x in parts]
            seq_old_to_new = seq_left_recent[::-1]
            for n in seq_old_to_new: append_timeline(n)
            update_ngrams()
        return {"ok": True, "analise": True}

    # 3) ENTRADA CONFIRMADA — gera sinal do canal (G0) e abre pendência p/ recuperação
    if not is_real_entry(t):
        return {"ok": True, "skipped": True}

    source_msg_id = msg.get("message_id")
    if query_one("SELECT 1 FROM suggestions WHERE source_msg_id=?", (source_msg_id,)):
        return {"ok": True, "dup": True}

    strategy  = extract_strategy(t) or ""
    seq_raw   = extract_seq_raw(t) or ""
    after_num = extract_after_num(t)
    base, pattern_key = parse_bases_and_pattern(t)
    if not base: base=[1,2,3,4]; pattern_key="GEN"

    number, conf, samples, gap = suggest_number_for_channel(base, pattern_key, after_num)
    if number is None:
        return {"ok": True, "skipped_low_conf": True}

    exec_write("""INSERT OR REPLACE INTO suggestions
                  (source_msg_id, strategy, seq_raw, context_key, pattern_key, base, suggested_number, stage, sent_at)
                  VALUES (?,?,?,?,?,?,?,?,?)""",
               (source_msg_id, strategy, seq_raw, "CTX", pattern_key, json.dumps(base), int(number), "G0", now_ts()))
    exec_write("""INSERT OR REPLACE INTO last_by_strategy
                  (strategy, source_msg_id, suggested_number, context_key, pattern_key, stage, created_at)
                  VALUES (?,?,?,?,?,?,?)""",
               (strategy, source_msg_id, int(number), "CTX", pattern_key, "G0", now_ts()))

    open_pending(strategy, int(number), source="CHAN")
    out = build_suggestion_msg(int(number), base, pattern_key, after_num, float(conf), int(samples))
    await tg_broadcast(out)
    return {"ok": True, "sent": True, "number": int(number), "conf": float(conf), "samples": int(samples)}

# =========================================
# DEBUG endpoints
# =========================================
@app.get("/debug/samples")
async def debug_samples():
    row = query_one("SELECT SUM(weight) AS s FROM ngram_stats")
    samples = int((row["s"] or 0) if row else 0)
    return {"samples": samples, "MIN_SAMPLES": MIN_SAMPLES, "enough_samples": samples >= MIN_SAMPLES}

_last_reason = "—"
@app.get("/debug/reason")
async def debug_reason():
    return {"last_reason": _last_reason}

@app.get("/debug/state")
async def debug_state():
    try:
        samples=int((_get_scalar("SELECT SUM(weight) FROM ngram_stats") or 0))
        pend_open=int(_get_scalar("SELECT COUNT(*) FROM pending_outcome WHERE open=1"))
        y=today_key_local()
        r=query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
        g0=(r["g0"] if r else 0); g1=(r["g1"] if r else 0); g2=(r["g2"] if r else 0); loss=(r["loss"] if r else 0)
        total=g0+g1+g2+loss
        acc=((g0+g1+g2)/total) if total else 0.0
        last_fire_age=None  # não há IA autônoma aqui
        return {
            "samples": samples,
            "enough_samples": samples >= MIN_SAMPLES,
            "pendencias_abertas": pend_open,
            "placar_hoje": {"g0": int(g0), "g1": int(g1), "g2": int(g2), "loss": int(loss), "acc_pct": round(acc*100, 2)},
            "ultimo_fire_ha_seconds": last_fire_age,
            "config": {
                "MIN_SAMPLES": MIN_SAMPLES, "MIN_CONF_G0": MIN_CONF_G0, "MIN_GAP_G0": MIN_GAP_G0,
                "WINDOW": WINDOW, "DECAY": DECAY
            }
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

_last_flush_ts=0
@app.get("/debug/flush")
async def debug_flush(request: Request, key: str = ""):
    global _last_flush_ts
    if not key or key != FLUSH_KEY: return {"ok": False, "error": "unauthorized"}
    now=now_ts()
    if now - (_last_flush_ts or 0) < 60:
        return {"ok": False,"error": "flush_cooldown","retry_after_seconds": 60 - (now - (_last_flush_ts or 0))}
    try: await tg_broadcast(_health_text())
    except Exception as e: print(f"[FLUSH] saúde erro: {e}")
    try: await send_scoreboard_30m()
    except Exception as e: print(f"[FLUSH] placar erro: {e}")
    _last_flush_ts=now
    return {"ok": True, "flushed": True}
