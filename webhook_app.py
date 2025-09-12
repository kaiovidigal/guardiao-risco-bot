# -*- coding: utf-8 -*-
# Fantan Guardião — FIRE-only (G0 + Recuperação oculta) — AJUSTADO
# Requisitos atendidos:
#   • Só dispara a mensagem curta (sem cabeçalho de IA, sem relatórios extras):
#       🎯 Número seco (G0): X
#       🧩 Padrão: <pattern> [após N]
#       🧮 Base: [ ... ]
#       📊 Conf: YY.YY% | Amostra≈Z
#   • Thresholds dinâmicos com warm-up iniciando em 0% e subindo conforme amostra e acc(7d).
#   • Mantém boost leve do top-2 da cauda (40).
#   • GREEN/LOSS continuam funcionando normalmente.
#   • Relatório de saúde reativado (sem texto de IA), com modo automático a cada X minutos
#     controlado por variáveis de ambiente, e disparo manual via /debug/flush.
#
# Rotas úteis preservadas (mesma estrutura):
#   POST /webhook/<WEBHOOK_TOKEN>
#   GET  /debug/state
#   GET  /debug/reason
#   GET  /debug/samples
#   GET  /debug/flush
#
import os, re, json, time, sqlite3, asyncio, shutil
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone, timedelta
from collections import Counter

import httpx
from fastapi import FastAPI, Request, HTTPException, Query
from pydantic import BaseModel

# =========================
# ENV / CONFIG
# =========================
DB_PATH = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
FLUSH_KEY      = os.getenv("FLUSH_KEY", "meusegredo123").strip()

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
# Espelho/replicador opcional (mantido, mas pode apontar para o seu canal principal)
REPL_ENABLED, REPL_CHANNEL = True, (os.getenv("REPL_CHANNEL") or "-1003052132833")

# Relatório de saúde: auto (1) ou só manual (0), e intervalo em minutos
HEALTH_AUTO = (os.getenv("HEALTH_AUTO", "1").strip() != "0")
HEALTH_EVERY_MIN = int(os.getenv("HEALTH_EVERY_MIN", "30") or "30")

# =========================
# MODO & Hiperparâmetros
# =========================
MAX_STAGE     = 3
FAST_RESULT   = True
SCORE_G0_ONLY = True

WINDOW = 400
DECAY  = 0.985
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12
ALPHA, BETA, GAMMA = 1.05, 0.70, 0.40
GAP_MIN = 0.08

# calibração base (mínimos — dinâmicos sobem com warm-up)
MIN_CONF_G0 = 0.55
MIN_GAP_G0  = 0.04
MIN_SAMPLES = 1000

# IA em disco (stub)
INTEL_DIR = os.getenv("INTEL_DIR", "/var/data/ai_intel").rstrip("/")
INTEL_MAX_BYTES = int(os.getenv("INTEL_MAX_BYTES", "1000000000"))
INTEL_SIGNAL_INTERVAL = float(os.getenv("INTEL_SIGNAL_INTERVAL", "20"))
INTEL_ANALYZE_INTERVAL = float(os.getenv("INTEL_ANALYZE_INTERVAL", "2"))  # ciclo rápido

SELF_LABEL_IA = os.getenv("SELF_LABEL_IA", "Tiro seco por IA")

# Caps e warm-up
CONF_CAP, GAP_CAP = 0.999, 1.0
WARMUP_SAMPLES    = 5_000

app = FastAPI(title="Fantan Guardião — FIRE-only (ajustado)", version="3.11.3")

# =========================
# DB helpers
# =========================
OLD_DB_CANDIDATES = ["/var/data/data.db","/opt/render/project/src/data.db",
                     "/opt/render/project/src/data/data.db","/data/data.db"]
def _ensure_db_dir():
    try: os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except Exception as e: print(f"[DB] mkdir: {e}")

def _migrate_old_db_if_needed():
    if os.path.exists(DB_PATH): return
    for src in OLD_DB_CANDIDATES:
        if os.path.exists(src):
            try: _ensure_db_dir(); shutil.copy2(src, DB_PATH); print(f"[DB] Migrado {src} -> {DB_PATH}"); return
            except Exception as e: print(f"[DB] Migração falhou {src}: {e}")

_ensure_db_dir(); _migrate_old_db_if_needed()

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
            if "locked" in str(e).lower() or "busy" in str(e).lower(): time.sleep(wait); continue
            raise
    raise sqlite3.OperationalError("Banco bloqueado após várias tentativas.")

def query_all(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
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
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score_ia (
        yyyymmdd TEXT PRIMARY KEY, g0 INTEGER NOT NULL DEFAULT 0, loss INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS recov_g1_stats (
        number INTEGER PRIMARY KEY, wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0)""")
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

# =========================
# Utils / Telegram
# =========================
def now_ts() -> int: return int(time.time())
def utc_iso() -> str: return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
def today_key_local() -> str: return datetime.now(timezone.utc).strftime("%Y%m%d")

async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True})

async def tg_broadcast(text: str, parse: str="HTML"):
    channel = REPL_CHANNEL or PUBLIC_CHANNEL
    if REPL_ENABLED and channel: await tg_send_text(channel, text, parse)

async def send_green_imediato(sugerido:int, stage_txt:str="G0"):
    await tg_broadcast(f"✅ <b>GREEN</b> em <b>{stage_txt}</b> — Número: <b>{sugerido}</b>")

async def send_loss_imediato(sugerido:int, stage_txt:str="G0"):
    await tg_broadcast(f"❌ <b>LOSS</b> — Número: <b>{sugerido}</b> (em {stage_txt})")

# IA — nunca publica cabeçalho; stub só mantém compatibilidade
async def ia2_send_signal(_best:int, _conf:float, _tail_len:int, _mode:str):
    return

# =========================
# Timeline & n-grams
# =========================
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
    tot = (query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key)) or {"w":0})["w"] or 0.0
    if tot <= 0: return 0.0
    w = (query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, candidate)) or {"weight":0})["weight"] or 0.0
    return w / tot

# =========================
# Parsers do canal
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
    m = re.search(r"Estrat[eé]gia:\s*(\d+)", text, flags=re.I); return m.group(1) if m else None

def extract_seq_raw(text: str) -> Optional[str]:
    m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", text, flags=re.I); return m.group(1).strip() if m else None

def extract_after_num(text: str) -> Optional[int]:
    m = re.search(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", text, flags=re.I); return int(m.group(1)) if m else None

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
        nums = [int(g) for g in m.groups() if g]; return nums, "SSH-" + "-".join(str(x) for x in nums)
    m = re.search(r"Sequ[eê]ncia:\s*([\d\s\|\-]+)", t, flags=re.I)
    if m:
        parts = re.findall(r"[1-4]", m.group(1)); base = bases_from_sequence_left_recent([int(x) for x in parts], 3)
        if base: return base, "SEQ"
    return [], "GEN"

# GREEN/RED — salva o ÚLTIMO número entre parênteses
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

# =========================
# Placar e métricas
# =========================
def laplace_ratio(wins:int, losses:int) -> float: return (wins + 1.0) / (wins + losses + 2.0)

def bump_pattern(pattern_key: str, number: int, won: bool):
    row = query_one("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pattern_key, number))
    w = (row["wins"] if row else 0) + (1 if won else 0)
    l = (row["losses"] if row else 0) + (0 if won else 1)
    exec_write("""
      INSERT INTO stats_pattern (pattern_key, number, wins, losses)
      VALUES (?,?,?,?)
      ON CONFLICT(pattern_key, number) DO UPDATE SET wins=excluded.wins, losses=excluded.losses
    """, (pattern_key, number, w, l))

def bump_strategy(strategy: str, number: int, won: bool):
    row = query_one("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?", (strategy, number))
    w = (row["wins"] if row else 0) + (1 if won else 0)
    l = (row["losses"] if row else 0) + (0 if won else 1)
    exec_write("""
      INSERT INTO stats_strategy (strategy, number, wins, losses)
      VALUES (?,?,?,?)
      ON CONFLICT(strategy, number) DO UPDATE SET wins=excluded.wins, losses=excluded.losses
    """, (strategy, number, w, l))

def update_daily_score(stage: Optional[int], won: bool):
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0,g1,g2,loss,streak = (row["g0"],row["g1"],row["g2"],row["loss"],row["streak"]) if row else (0,0,0,0,0)
    if SCORE_G0_ONLY:
        if won and stage == 0: g0 += 1; streak += 1
        elif not won: loss += 1; streak = 0
    else:
        if won:
            (g0,g1,g2)[stage] += 1
            streak += 1
        else: loss += 1; streak = 0
    exec_write("""INSERT INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(yyyymmdd) DO UPDATE SET g0=excluded.g0,g1=excluded.g1,g2=excluded.g2,loss=excluded.loss,streak=excluded.streak""",
               (y,g0,g1,g2,loss,streak))
    total = g0 + loss
    acc = (g0/total) if total else 0.0
    return g0, loss, acc, streak

def update_daily_score_ia(stage: Optional[int], won: bool):
    y = today_key_local()
    row = query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    g0,loss,streak = (row["g0"],row["loss"],row["streak"]) if row else (0,0,0)
    if won and stage == 0: g0 += 1; streak += 1
    elif not won: loss += 1; streak = 0
    exec_write("""INSERT INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,?,?,?)
                   ON CONFLICT(yyyymmdd) DO UPDATE SET g0=excluded.g0,loss=excluded.loss,streak=excluded.streak""",
               (y,g0,loss,streak))
    total = g0 + loss
    acc = (g0/total) if total else 0.0
    return g0, loss, acc, streak

def _convert_last_loss_to_green():
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    if not row:
        exec_write("""INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,0,0,0,0,0)""",(y,)); 
        return
    g0,loss,streak = row["g0"] or 0, row["loss"] or 0, row["streak"] or 0
    if loss>0: loss-=1; g0+=1; streak+=1
    exec_write("""INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,?,?,?,?,?)""",
               (y,g0,row["g1"] or 0,row["g2"] or 0,loss,streak))

def _convert_last_loss_to_green_ia():
    y = today_key_local()
    row = query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    if not row:
        exec_write("""INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,0,0,0)""",(y,)); 
        return
    g0,loss,streak = row["g0"] or 0, row["loss"] or 0, row["streak"] or 0
    if loss>0: loss-=1; g0+=1; streak+=1
    exec_write("""INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,?,?,?)""",
               (y,g0,loss,streak))

def _ia_acc_days(days:int=7) -> Tuple[int,int,float]:
    days = max(1, min(int(days), 30))
    rows = query_all("""SELECT g0, loss FROM daily_score_ia ORDER BY yyyymmdd DESC LIMIT ?""",(days,))
    g = sum((r["g0"] or 0) for r in rows); l = sum((r["loss"] or 0) for r in rows)
    return int(g), int(l), (g/(g+l)) if (g+l)>0 else 0.0

# =========================
# Health + debug (relatório curto, sem texto de IA)
# =========================
def _get_scalar(sql: str, params: tuple = (), default: int|float = 0):
    row = query_one(sql, params)
    if not row: return default
    try: return row[0] if row[0] is not None else default
    except: keys=row.keys(); return row[keys[0]] if keys and row[keys[0]] is not None else default

_ia2_last_reason, _ia2_last_reason_ts = "—", 0
def _mark_reason(r: str):
    global _ia2_last_reason,_ia2_last_reason_ts
    _ia2_last_reason, _ia2_last_reason_ts = r, now_ts()

def _health_text() -> str:
    try:
        samples = int((query_one("SELECT SUM(weight) AS s FROM ngram_stats")["s"] or 0))
    except: 
        samples = 0

    pend_open = int(_get_scalar("SELECT COUNT(*) FROM pending_outcome WHERE open=1"))
    y = today_key_local()
    r = query_one("SELECT g0,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0   = int(r["g0"]   if r and r["g0"]   is not None else 0)
    loss = int(r["loss"] if r and r["loss"] is not None else 0)
    total = g0 + loss
    acc = (g0/total*100.0) if total else 0.0

    age = max(0, now_ts() - (_ia2_last_reason_ts or now_ts()))
    return ("🩺 <b>Saúde do Guardião</b>\n"
            f"⏱️ UTC: <code>{utc_iso()}</code>\n"
            f"📚 amostras≈<b>{samples}</b>\n"
            f"📊 Hoje: G0=<b>{g0}</b> | Loss=<b>{loss}</b> | ✅ {acc:.2f}%\n"
            f"⏳ pendências abertas: <b>{pend_open}</b>\n"
            f"⚙️ conf≥{MIN_CONF_G0:.2f} | gap≥{MIN_GAP_G0:.3f}\n"
            f"🧠 motivo: {_ia2_last_reason} (há {age}s)\n")

async def _health_reporter_task():
    # envia a cada HEALTH_EVERY_MIN (padrão 30 min)
    while True:
        try:
            await tg_broadcast(_health_text())
        except Exception as e:
            print(f"[HEALTH] erro: {e}")
        await asyncio.sleep(max(60, HEALTH_EVERY_MIN * 60))

async def _daily_reset_task():
    while True:
        try:
            now = datetime.now(timezone.utc)
            tomorrow = (now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
            await asyncio.sleep(max(1.0,(tomorrow-now).total_seconds()))
            y = today_key_local()
            exec_write("""INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,0,0,0,0,0)""",(y,))
            exec_write("""INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,0,0,0)""",(y,))
        except Exception as e:
            print(f"[RESET] erro: {e}")
            await asyncio.sleep(60)

# =========================
# Top-2 cauda (40) — boost leve
# =========================
def tail_top2_boost(tail: List[int], k:int=40) -> Dict[int, float]:
    boosts={1:1.00,2:1.00,3:1.00,4:1.00}
    if not tail: return boosts
    tail_k = tail[-k:] if len(tail)>=k else tail[:]
    c=Counter(tail_k); freq=c.most_common()
    if len(freq)>=1: boosts[freq[0][0]]=1.04
    if len(freq)>=2: boosts[freq[1][0]]=1.02
    return boosts

# =========================
# Predição (número seco)
# =========================
def ngram_backoff_score(tail: List[int], after_num: Optional[int], candidate: int) -> float:
    if not tail: return 0.0
    if after_num is not None:
        idxs=[i for i,v in enumerate(tail) if v==after_num]
        if not idxs:
            ctx4=tail[-4:] if len(tail)>=4 else []; ctx3=tail[-3:] if len(tail)>=3 else []
            ctx2=tail[-2:] if len(tail)>=2 else [];  ctx1=tail[-1:] if len(tail)>=1 else []
        else:
            i=idxs[-1]
            ctx1=tail[max(0,i):i+1]; ctx2=tail[max(0,i-1):i+1] if i-1>=0 else []
            ctx3=tail[max(0,i-2):i+1] if i-2>=0 else []; ctx4=tail[max(0,i-3):i+1] if i-3>=0 else []
    else:
        ctx4=tail[-4:] if len(tail)>=4 else []; ctx3=tail[-3:] if len(tail)>=3 else []
        ctx2=tail[-2:] if len(tail)>=2 else [];  ctx1=tail[-1:] if len(tail)>=1 else []
    parts=[]
    if len(ctx4)==4: parts.append((W4, prob_from_ngrams(ctx4[:-1], candidate)))
    if len(ctx3)==3: parts.append((W3, prob_from_ngrams(ctx3[:-1], candidate)))
    if len(ctx2)==2: parts.append((W2, prob_from_ngrams(ctx2[:-1], candidate)))
    if len(ctx1)==1: parts.append((W1, prob_from_ngrams(ctx1[:-1], candidate)))
    return sum(w*p for w,p in parts)

def confident_best(post: Dict[int,float], gap: float = GAP_MIN) -> Optional[int]:
    a=sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a: return None
    if len(a)==1: return a[0][0]
    return a[0][0] if (a[0][1]-a[1][1]) >= gap else None

def suggest_number(base: List[int], pattern_key: str, strategy: Optional[str], after_num: Optional[int]) -> Tuple[Optional[int],float,int,Dict[int,float]]:
    if not base: base=[1,2,3,4]
    hour_block=int(datetime.now(timezone.utc).hour // 2)
    pat_key=f"{pattern_key}|h{hour_block}"

    tail=get_recent_tail(WINDOW); scores: Dict[int,float]={}

    if after_num is not None:
        try: last_idx=max([i for i,v in enumerate(tail) if v==after_num])
        except ValueError: return None, 0.0, len(tail), {k:1/len(base) for k in base}
        if (len(tail)-1 - last_idx) > 60: return None, 0.0, len(tail), {k:1/len(base) for k in base}

    boost_tail = tail_top2_boost(tail, k=40)

    for c in base:
        ng = ngram_backoff_score(tail, after_num, c)
        rowp = query_one("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pat_key, c))
        pw,pl = (rowp["wins"] if rowp else 0),(rowp["losses"] if rowp else 0)
        p_pat = laplace_ratio(pw, pl)

        p_str = 1/len(base)
        if strategy:
            rows = query_one("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?", (strategy, c))
            sw,sl = (rows["wins"] if rows else 0),(rows["losses"] if rows else 0)
            p_str = laplace_ratio(sw, sl)

        boost_pat = 1.05 if p_pat>=0.60 else (0.97 if p_pat<=0.40 else 1.00)
        prior = 1.0/len(base)
        score = prior * ((ng or 1e-6) ** ALPHA) * (p_pat ** BETA) * (p_str ** GAMMA) * boost_pat * boost_tail.get(c,1.00)
        scores[c]=score

    total=sum(scores.values()) or 1e-9
    post={k:v/total for k,v in scores.items()}
    number=confident_best(post, gap=GAP_MIN)
    conf = post.get(number, 0.0) if number is not None else 0.0

    roww=query_one("SELECT SUM(weight) AS s FROM ngram_stats"); samples=int((roww["s"] or 0) if roww else 0)

    # anti-repique
    last = query_one("SELECT suggested, announced FROM pending_outcome ORDER BY id DESC LIMIT 1")
    if last and number is not None and last["suggested"]==number and (last["announced"] or 0)==1:
        if post.get(number,0.0) < (MIN_CONF_G0 + 0.08): return None, 0.0, samples, post

    # evita empate técnico
    top=sorted(post.items(), key=lambda kv: kv[1], reverse=True)[:2]
    if len(top)==2 and (top[0][1]-top[1][1])<0.015: return None, 0.0, samples, post

    return number, conf, samples, post

# =========================
# IA2 — processo
# =========================
IA2_BLOCK_UNTIL=0; IA2_SENT_THIS_HOUR=0; IA2_HOUR_BUCKET=None; IA2_LAST_FIRE_TS=0
def _ia2_hour_key() -> int: return int(datetime.now(timezone.utc).strftime("%Y%m%d%H"))
def _ia2_reset_hour():
    global IA2_SENT_THIS_HOUR, IA2_HOUR_BUCKET
    hb=_ia2_hour_key()
    if IA2_HOUR_BUCKET!=hb: IA2_HOUR_BUCKET=hb; IA2_SENT_THIS_HOUR=0
def _ia2_antispam_ok() -> bool:
    _ia2_reset_hour(); return IA2_SENT_THIS_HOUR < 20
def _ia2_mark_sent():
    global IA2_SENT_THIS_HOUR; IA2_SENT_THIS_HOUR += 1
def _ia2_blocked_now() -> bool: return now_ts() < IA2_BLOCK_UNTIL
def _ia_set_post_loss_block():
    global IA2_BLOCK_UNTIL; IA2_BLOCK_UNTIL = now_ts() + 12
def _ia2_can_fire_now() -> bool:
    if _ia2_blocked_now(): return False
    if not _ia2_antispam_ok(): return False
    if now_ts() - (IA2_LAST_FIRE_TS or 0) < 10: return False
    return True
def _ia2_mark_fire_sent():
    global IA2_LAST_FIRE_TS; _ia2_mark_sent(); IA2_LAST_FIRE_TS=now_ts()

def _dyn_thresholds() -> tuple[float, float]:
    """ Thresholds dinâmicos: começam em 0%/0.0 no warm-up e sobem com acc(7d)+amostra. """
    g7,l7,acc7=_ia_acc_days(7)
    row = query_one("SELECT SUM(weight) AS s FROM ngram_stats")
    samples=int((row["s"] or 0) if row else 0)
    if samples < WARMUP_SAMPLES:
        base_conf, base_gap = 0.00, 0.000
    else:
        base_conf = 0.50 + min(0.10, acc7*0.12) + min(0.05, (samples/200_000.0)*0.05)
        base_gap  = 0.030 + min(0.02, acc7*0.025) + min(0.010,(samples/100_000.0)*0.010)
    min_conf = max(base_conf, MIN_CONF_G0); min_gap = max(base_gap, MIN_GAP_G0)
    min_conf = min(min_conf, 0.68); min_gap = min(min_gap, 0.060)
    return round(min_conf,3), round(min_gap,3)

def _relevance_ok(conf_raw: float, gap: float, tail_len: int) -> bool:
    min_conf_dyn, min_gap_dyn = _dyn_thresholds()
    conf_c = max(0.0, min(float(conf_raw), CONF_CAP))
    gap_c  = max(0.0, min(float(gap),      GAP_CAP))
    return (conf_c >= min_conf_dyn) and (gap_c >= min_gap_dyn) and (tail_len >= MIN_SAMPLES)

def open_pending(strategy: Optional[str], suggested: int, source: str = "CHAN"):
    exec_write("""INSERT INTO pending_outcome (created_at,strategy,suggested,stage,open,window_left,seen_numbers,announced,source)
                  VALUES (?,?,?,?,1,?, '', 0, ?)""", (now_ts(), strategy or "", int(suggested), 0, MAX_STAGE, source))

async def close_pending_with_result(n_observed: int, _event_kind: str):
    rows=query_all("""SELECT id, strategy, suggested, stage, window_left, seen_numbers, announced, source
                      FROM pending_outcome WHERE open=1 ORDER BY id ASC""")
    if not rows: return {"ok": True, "no_open": True}
    for r in rows:
        pid, suggested, stage, left = r["id"], int(r["suggested"]), int(r["stage"]), int(r["window_left"])
        src = (r["source"] or "CHAN").upper()
        seen_new = ((r["seen_numbers"] or "") + ("," if (r["seen_numbers"] or "") else "") + str(int(n_observed))).strip()
        if int(n_observed)==suggested:
            exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?", (seen_new, pid))
            if stage==0:
                update_daily_score(0, True)
                if src=="IA": update_daily_score_ia(0, True)
                await send_green_imediato(suggested,"G0")
            else:
                _convert_last_loss_to_green()
                if src=="IA": _convert_last_loss_to_green_ia()
        else:
            if left>1:
                exec_write("""UPDATE pending_outcome SET stage=stage+1, window_left=window_left-1, seen_numbers=? WHERE id=?""",(seen_new,pid))
                if stage==0:
                    update_daily_score(0, False)
                    if src=="IA": update_daily_score_ia(0, False)
                    await send_loss_imediato(suggested,"G0"); _ia_set_post_loss_block()
            else:
                exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?", (seen_new, pid))
    return {"ok": True}

async def ia2_process_once():
    base, pattern_key, strategy, after_num = [], "GEN", None, None
    tail = get_recent_tail(WINDOW)
    best, conf_raw, tail_len, post = suggest_number(base, pattern_key, strategy, after_num)

    # gap atual
    gap = 1.0
    if post and len(post) >= 2:
        top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
        if len(top) >= 2: gap = top[0][1] - top[1][1]

    # se já há pendência, não abre outra
    rows=query_all("SELECT COUNT(*) AS c FROM pending_outcome WHERE open=1")
    if rows and (rows[0]["c"] or 0) > 0:
        _mark_reason("pendencia_aberta"); return

    # bloqueios básicos
    if _ia2_blocked_now(): _mark_reason("cooldown_pos_loss"); return
    if not _ia2_antispam_ok(): _mark_reason("limite_hora_atingido"); return
    if now_ts() - (IA2_LAST_FIRE_TS or 0) < 10:
        _mark_reason("espacamento_minimo"); return

    # Caso 1: temos best e passa nos thresholds dinâmicos — abre pendência (não publica cabeçalho)
    if best is not None and _relevance_ok(conf_raw, gap, tail_len) and _ia2_can_fire_now():
        open_pending(strategy, best, source="IA")
        _ia2_mark_fire_sent()
        _mark_reason(f"FIRE(best={best}, conf={conf_raw:.3f}, gap={gap:.3f}, tail={tail_len})")
        return

    # Caso 2: WARMUP exploratório (sem publicação)
    if tail_len < WARMUP_SAMPLES and _ia2_can_fire_now():
        _ia2_mark_fire_sent()
        _mark_reason(f"WARMUP(skip_publish)({tail_len})")
        return

    # Caso 3: não disparou
    if tail_len < MIN_SAMPLES: _mark_reason(f"amostra_insuficiente({tail_len}<{MIN_SAMPLES})")
    else: _mark_reason(f"reprovado(conf={conf_raw:.3f}, gap={gap:.3f})")

# =========================
# INTEL (stub)
# =========================
class _IntelStub:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(os.path.join(self.base_dir, "snapshots"), exist_ok=True)
    def start_signal(self, suggested: int, strategy: Optional[str] = None):
        try:
            path = os.path.join(self.base_dir, "snapshots", "latest_top.json")
            with open(path, "w") as f: json.dump({"ts": now_ts(), "suggested": suggested, "strategy": strategy}, f)
        except Exception as e: print(f"[INTEL] snapshot error: {e}")
    def stop_signal(self): pass

INTEL = _IntelStub(INTEL_DIR)

# =========================
# Webhook models & rotas
# =========================
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
    try:
        if HEALTH_AUTO:
            asyncio.create_task(_health_reporter_task())
    except Exception as e: print(f"[HEALTH] startup error: {e}")
    try: asyncio.create_task(_daily_reset_task())
    except Exception as e: print(f"[RESET] startup error: {e}")
    async def _ia2_loop():
        while True:
            try: await ia2_process_once()
            except Exception as e: print(f"[IA2] analyzer error: {e}")
            await asyncio.sleep(max(0.2, INTEL_ANALYZE_INTERVAL))
    try: asyncio.create_task(_ia2_loop())
    except Exception as e: print(f"[IA2] startup error: {e}")

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN: raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg: return {"ok": True}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    t = re.sub(r"\s+", " ", text)

    # 1) GREEN / RED — salva o ÚLTIMO número do parênteses
    gnum = extract_green_number(t)
    redn = extract_red_last_left(t)
    if gnum is not None or redn is not None:
        n_observed = gnum if gnum is not None else redn
        append_timeline(n_observed)
        update_ngrams()
        await close_pending_with_result(n_observed, "GREEN" if gnum is not None else "RED")

        strat = extract_strategy(t) or ""
        row = query_one("SELECT suggested_number, pattern_key FROM last_by_strategy WHERE strategy=?", (strat,))
        if row and gnum is not None:
            suggested = int(row["suggested_number"]); pat_key = row["pattern_key"] or "GEN"
            won = (suggested == int(gnum))
            bump_pattern(pat_key, suggested, won)
            if strat: bump_strategy(strat, suggested, won)

        await ia2_process_once()
        return {"ok": True, "observed": n_observed}

    # 2) ANALISANDO — só registra sequência, sem sinal (avisar que está analisando)
    if is_analise(t):
        seq_raw = extract_seq_raw(t)
        if seq_raw:
            parts = re.findall(r"[1-4]", seq_raw)
            seq_left_recent = [int(x) for x in parts]
            seq_old_to_new  = seq_left_recent[::-1]
            for n in seq_old_to_new: append_timeline(n)
            update_ngrams()
        # status curto (opcional) — pode comentar se não quiser esse aviso
        await tg_broadcast("🟡 Analisando... possível entrada em breve.")
        await ia2_process_once()
        return {"ok": True, "analise": True}

    # 3) ENTRADA CONFIRMADA — processa G0 (pode abster por qualidade)
    if not is_real_entry(t):
        await ia2_process_once()
        return {"ok": True, "skipped": True}

    source_msg_id = msg.get("message_id")
    if query_one("SELECT 1 FROM suggestions WHERE source_msg_id=?", (source_msg_id,)):
        await ia2_process_once()
        return {"ok": True, "dup": True}

    strategy  = extract_strategy(t) or ""
    seq_raw   = extract_seq_raw(t) or ""
    after_num = extract_after_num(t)
    base, pattern_key = parse_bases_and_pattern(t)
    if not base: base=[1,2,3,4]; pattern_key="GEN"

    number, conf, samples, post = suggest_number(base, pattern_key, strategy, after_num)

    # thresholds dinâmicos
    min_conf_dyn, min_gap_dyn = _dyn_thresholds()
    gap = 1.0
    if post and len(post) >= 2:
        top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
        if len(top) >= 2: gap = top[0][1] - top[1][1]

    if number is None or conf < min_conf_dyn or gap < min_gap_dyn or samples < MIN_SAMPLES:
        await ia2_process_once()
        return {"ok": True, "skipped_low_conf": True, "conf_min": min_conf_dyn, "gap_min": min_gap_dyn}

    exec_write("""INSERT OR REPLACE INTO suggestions
                  (source_msg_id, strategy, seq_raw, context_key, pattern_key, base, suggested_number, stage, sent_at)
                  VALUES (?,?,?,?,?,?,?,?,?)""",
               (source_msg_id, strategy, seq_raw, "CTX", pattern_key, json.dumps(base), int(number), "G0", now_ts()))
    exec_write("""INSERT OR REPLACE INTO last_by_strategy
                  (strategy, source_msg_id, suggested_number, context_key, pattern_key, stage, created_at)
                  VALUES (?,?,?,?,?,?,?)""",
               (strategy, source_msg_id, int(number), "CTX", pattern_key, "G0", now_ts()))

    open_pending(strategy, int(number), source="CHAN")
    conf_capped = max(0.0, min(float(conf), CONF_CAP))
    out_lines = [
        f"🎯 <b>Número seco (G0):</b> <b>{int(number)}</b>",
        f"🧩 <b>Padrão:</b> {pattern_key}{(' após '+str(after_num)) if after_num else ''}",
        f"🧮 <b>Base:</b> [{', '.join(str(x) for x in base)}]",
        f"📊 Conf: {conf_capped*100:.2f}% | Amostra≈{samples}"
    ]
    await tg_broadcast("\n".join(out_lines))

    await ia2_process_once()
    return {"ok": True, "sent": True, "number": int(number), "conf": float(conf_capped), "samples": int(samples)}

# =========================
# DEBUG endpoints (com envio de relatório de saúde no flush manual)
# =========================
@app.get("/debug/samples")
async def debug_samples():
    row = query_one("SELECT SUM(weight) AS s FROM ngram_stats")
    samples = int((row["s"] or 0) if row else 0)
    return {"samples": samples, "MIN_SAMPLES": MIN_SAMPLES, "enough_samples": samples >= MIN_SAMPLES}

@app.get("/debug/reason")
async def debug_reason():
    try:
        age=max(0, now_ts() - (_ia2_last_reason_ts or now_ts()))
        return {"last_reason": _ia2_last_reason, "last_reason_age_seconds": age}
    except Exception as e: return {"error": str(e)}

@app.get("/debug/state")
async def debug_state():
    try:
        samples=int((query_one("SELECT SUM(weight) AS s FROM ngram_stats")["s"] or 0))
        pend_open=int(_get_scalar("SELECT COUNT(*) FROM pending_outcome WHERE open=1"))
        reason_age=max(0, now_ts() - (_ia2_last_reason_ts or now_ts()))
        y=today_key_local()
        r=query_one("SELECT g0,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
        g0=(r["g0"] if r else 0); loss=(r["loss"] if r else 0); acc=(g0/(g0+loss)) if (g0+loss)>0 else 0.0
        ria=query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
        ia_g0=(ria["g0"] if ria else 0); ia_loss=(ria["loss"] if ria else 0)
        ia_acc=(ia_g0/(ia_g0+ia_loss)) if (ia_g0+ia_loss)>0 else 0.0
        cooldown_remaining=max(0, IA2_BLOCK_UNTIL - now_ts())
        last_fire_age=max(0, now_ts() - (IA2_LAST_FIRE_TS or 0))
        return {
            "samples": samples,
            "enough_samples": samples >= MIN_SAMPLES,
            "pendencias_abertas": pend_open,
            "last_reason": _ia2_last_reason,
            "last_reason_age_seconds": int(reason_age),
            "cooldown_pos_loss_seconds": int(cooldown_remaining),
            "ultimo_fire_ha_seconds": int(last_fire_age),
            "placar_canal_hoje": {"g0": int(g0), "loss": int(loss), "acc_pct": round(acc*100, 2)},
            "placar_ia_hoje": {"g0": int(ia_g0), "loss": int(ia_loss), "acc_pct": round(ia_acc*100, 2)},
            "config": {
                "MIN_SAMPLES": MIN_SAMPLES,
                "MIN_CONF_G0": MIN_CONF_G0, "MIN_GAP_G0": MIN_GAP_G0,
                "IA2_MAX_PER_HOUR": 20, "IA2_MIN_SECONDS_BETWEEN_FIRE": 10,
                "IA2_COOLDOWN_AFTER_LOSS": 12, "MAX_CONCURRENT_PENDINGS": 2,
                "WARMUP_SAMPLES": WARMUP_SAMPLES,
                "HEALTH_AUTO": HEALTH_AUTO, "HEALTH_EVERY_MIN": HEALTH_EVERY_MIN
            }
        }
    except Exception as e: return {"ok": False, "error": str(e)}

_last_flush_ts=0
@app.get("/debug/flush")
async def debug_flush(days: int = 7, key: str = Query(default="")):
    global _last_flush_ts
    try:
        if not key or key != FLUSH_KEY: return {"ok": False, "error": "unauthorized"}
        now=now_ts()
        if now - (_last_flush_ts or 0) < 60:
            return {"ok": False,"error": "flush_cooldown","retry_after_seconds": 60 - (now - (_last_flush_ts or 0))}
        _last_flush_ts=now
        # Envia o relatório curto de saúde
        try:
            await tg_broadcast(_health_text())
        except Exception as e:
            print(f"[FLUSH] erro ao enviar saúde: {e}")
        return {"ok": True, "flushed": True, "days": max(1,min(int(days),30))}
    except Exception as e: return {"ok": False, "error": str(e)}
