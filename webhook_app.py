# -*- coding: utf-8 -*-
# Fan Tan - Guardiao (G0 + Recuperacao)
# - Inteligencia em disco: /var/data/ai_intel (teto 1 GB + rotacao)
# - Batidas do sinal a cada 20s enquanto houver pendencia aberta
# - Analisador periodico a cada 2s com top combos recentes da cauda
#
# Rotas:
#   POST /webhook/<WEBHOOK_TOKEN>   (recebe mensagens do Telegram)
#   GET  /                          (ping)
#
# ENV obrigatorias:
#   TG_BOT_TOKEN, PUBLIC_CHANNEL, WEBHOOK_TOKEN
#
# ENV opcionais:
#   DB_PATH (default: /data/data.db)
#   INTEL_DIR (default: /var/data/ai_intel)
#   INTEL_MAX_BYTES (default: 1000000000 -> 1 GB)
#   INTEL_SIGNAL_INTERVAL (default: 20)
#   INTEL_ANALYZE_INTERVAL (default: 2)
#
# AUTO_FIRE=1                       -> habilita "Tiro seco por IA"
# SELF_TH_G0=0.90                   -> (LEGADO) confianca minima G0 para IA (nao usado)
# SELF_REQUIRE_G1=1                 -> (LEGADO) exigir G1 100% no lookback (nao usado)
# SELF_G1_LOOKBACK=50               -> (LEGADO) lookback (compatibilidade)
# SELF_COOLDOWN_S=6                 -> cooldown de auto-disparo
# SELF_LABEL_IA="Tiro seco por IA"  -> rotulo do auto-sinal
#
# NOVO (Off-base Prior):
#   ALLOW_OFFBASE=1                 -> avalia candidatos 1..4 mesmo fora da base do padrao
#   OFFBASE_PRIOR_FRACTION=0.25     -> fracao do prior reservada ao off-base (0.0-0.9)
#
# NOVO (Explore / Risco / Rotulagem):
#   EXPLORE_SIGNALS=5
#   MIN_SAMPLES_FOR_SIGNAL=30
#   MAX_SIGNALS_PER_HOUR=6
#   DAILY_STOP_LOSS=5
#   COOLDOWN_AFTER_LOSS=60
#   SHOW_MODE_TAG=1

import os, re, json, time, sqlite3, asyncio, shutil
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# ENV / CONFIG
# =========================
DB_PATH = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"

# Migracao automatica do DB antigo (preserva aprendizado)
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
        print(f"[DB] Falha ao criar dir DB: {e}")

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
                print(f"[DB] Erro migrando {src} -> {DB_PATH}: {e}")

_ensure_db_dir()
_migrate_old_db_if_needed()

TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Canal de replica (fixo)
REPL_ENABLED  = True
REPL_CHANNEL  = "-1003052132833"  # @fantanautomatico2

# =========================
# MODO: Resultado rapido com recuperacao
# =========================
MAX_STAGE      = 3          # 3 = G0, G1, G2 (recuperacao)
FAST_RESULT    = True       # publica G0 imediatamente; G1/G2 so informam "Recuperou" se bater
SCORE_G0_ONLY  = True       # placar do dia so conta G0 e Loss (limpo)

# =========================
# Hiperparametros (ajuste G0 equilibrado)
# =========================
WINDOW = 400
DECAY  = 0.985
# Pesos (menos overfit em contexto longo)
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12
ALPHA, BETA, GAMMA = 1.05, 0.70, 0.40
GAP_MIN = 0.08

# Filtros de qualidade para G0 (modo equilibrado)
MIN_CONF_G0 = 0.55      # confianca minima do top1
MIN_GAP_G0  = 0.04      # distancia top1 - top2
MIN_SAMPLES = 20000     # so aplica filtros rigidos quando ha amostra suficiente

# =========================
# Inteligencia em disco (/var/data) - ENV
# =========================
INTEL_DIR = os.getenv("INTEL_DIR", "/var/data/ai_intel").rstrip("/")
INTEL_MAX_BYTES = int(os.getenv("INTEL_MAX_BYTES", "1000000000"))  # 1 GB
INTEL_SIGNAL_INTERVAL = float(os.getenv("INTEL_SIGNAL_INTERVAL", "20"))
INTEL_ANALYZE_INTERVAL = float(os.getenv("INTEL_ANALYZE_INTERVAL", "2"))

# =========================
# Auto Sinal (legados mantidos p/ compatibilidade)
# =========================
AUTO_FIRE          = (os.getenv("AUTO_FIRE", "1").strip() == "1")
SELF_TH_G0         = float(os.getenv("SELF_TH_G0", "0.90"))
SELF_REQUIRE_G1    = (os.getenv("SELF_REQUIRE_G1", "1").strip() == "1")
SELF_G1_LOOKBACK   = int(os.getenv("SELF_G1_LOOKBACK", "50"))
SELF_COOLDOWN_S    = float(os.getenv("SELF_COOLDOWN_S", "6"))
SELF_LABEL_IA      = os.getenv("SELF_LABEL_IA", "Tiro seco por IA")

# Explore / Risco / Rotulagem
EXPLORE_SIGNALS        = int(os.getenv("EXPLORE_SIGNALS", "5"))
MIN_SAMPLES_FOR_SIGNAL = int(os.getenv("MIN_SAMPLES_FOR_SIGNAL", "30"))
MAX_SIGNALS_PER_HOUR   = int(os.getenv("MAX_SIGNALS_PER_HOUR", "6"))
DAILY_STOP_LOSS        = int(os.getenv("DAILY_STOP_LOSS", "5"))
COOLDOWN_AFTER_LOSS    = float(os.getenv("COOLDOWN_AFTER_LOSS", "60"))
SHOW_MODE_TAG          = (os.getenv("SHOW_MODE_TAG", "1").strip() == "1")

# Off-base Prior
ALLOW_OFFBASE = (os.getenv("ALLOW_OFFBASE", "1").strip() == "1")
OFFBASE_PRIOR_FRACTION = float(os.getenv("OFFBASE_PRIOR_FRACTION", "0.25"))

app = FastAPI(title="Fantan Guardiao — FastResult (G0 + Recuperacao G1/G2)", version="3.5.0")

# ===== IA G0-only (sem ENV, sem bloqueio por G1) =====
IA2_TIER_STRICT          = 0.60   # abre janela (FIRE) se conf_raw >= 0.60
IA2_TIER_SOFT            = 0.40   # abaixo disso so INFO
IA2_GAP_SAFETY           = 0.08   # salva FIRE com gap grande (top1-top2)
IA2_DELTA_GAP            = 0.03   # tolerancia no FIRE quando gap for grande
IA2_MAX_PER_HOUR         = 10     # antispam/hora (so FIRE)
IA2_COOLDOWN_AFTER_LOSS  = 20     # segundos de respiro apos 1 loss (so FIRE)

# Estado volatil (memoria)
_ia2_blocked_until_ts: int = 0
_ia2_sent_this_hour: int = 0
_ia2_hour_bucket: Optional[int] = None

# =========================
# SQLite helpers (WAL + timeout + retry)
# =========================
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
            con = _connect()
            con.execute(sql, params)
            con.commit()
            con.close()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                time.sleep(wait)
                continue
            raise
    raise sqlite3.OperationalError("Banco bloqueado apos varias tentativas.")

def query_all(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    con = _connect()
    rows = con.execute(sql, params).fetchall()
    con.close()
    return rows

def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    con = _connect()
    row = con.execute(sql, params).fetchone()
    con.close()
    return row

# =========================
# Init DB + migracoes
# =========================
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
    cur.execute("""CREATE TABLE IF NOT EXISTS stats_pattern (
        pattern_key TEXT NOT NULL, number INTEGER NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (pattern_key, number)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS stats_strategy (
        strategy TEXT NOT NULL, number INTEGER NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (strategy, number)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS suggestions (
        source_msg_id INTEGER PRIMARY KEY, strategy TEXT, seq_raw TEXT,
        context_key TEXT, pattern_key TEXT, base TEXT,
        suggested_number INTEGER, stage TEXT, sent_at INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS last_by_strategy (
        strategy TEXT PRIMARY KEY, source_msg_id INTEGER, suggested_number INTEGER,
        context_key TEXT, pattern_key TEXT, stage TEXT, created_at INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY,
        g0 INTEGER NOT NULL DEFAULT 0,
        g1 INTEGER NOT NULL DEFAULT 0,
        g2 INTEGER NOT NULL DEFAULT 0,
        loss INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_outcome (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
        strategy TEXT, suggested INTEGER NOT NULL, stage INTEGER NOT NULL,
        open INTEGER NOT NULL, window_left INTEGER NOT NULL,
        seen_numbers TEXT DEFAULT '',
        announced INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'CHAN'
    )""")
    # Placar exclusivo IA (G0/Loss + streak)
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score_ia (
        yyyymmdd TEXT PRIMARY KEY,
        g0 INTEGER NOT NULL DEFAULT 0,
        loss INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0
    )""")
    # Estatistica de recuperacao em G1
    cur.execute("""CREATE TABLE IF NOT EXISTS recov_g1_stats (
        number INTEGER PRIMARY KEY,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0
    )""")
    # Parametros dinamicos (compat)
    cur.execute("""CREATE TABLE IF NOT EXISTS ia_params (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    con.commit()

    # Migracoes idempotentes
    try:
        cur.execute("ALTER TABLE pending_outcome ADD COLUMN seen_numbers TEXT DEFAULT ''")
        con.commit()
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE pending_outcome ADD COLUMN announced INTEGER NOT NULL DEFAULT 0")
        con.commit()
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE pending_outcome ADD COLUMN source TEXT NOT NULL DEFAULT 'CHAN'")
        con.commit()
    except sqlite3.OperationalError:
        pass

    con.close()

init_db()

# =========================
# Utils / Telegram
# =========================
def now_ts() -> int:
    return int(time.time())

def utc_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")

async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse,
                "disable_web_page_preview": True
            },
        )

async def tg_broadcast(text: str, parse: str="HTML"):
    if REPL_ENABLED and REPL_CHANNEL:
        await tg_send_text(REPL_CHANNEL, text, parse)

# === Mensagens ===
async def send_green_imediato(sugerido:int, stage_txt:str="G0"):
    await tg_broadcast(f"✅ <b>GREEN</b> em <b>{stage_txt}</b> — Número: <b>{sugerido}</b>")

async def send_loss_imediato(sugerido:int, stage_txt:str="G0"):
    await tg_broadcast(f"❌ <b>LOSS</b> — Número: <b>{sugerido}</b> (em {stage_txt})")

async def send_recovery(stage_txt:str):
    await tg_broadcast(f"♻️ Recuperou em <b>{stage_txt}</b> (GREEN)")

async def ia2_send_signal(best:int, conf:float, tail_len:int, mode:str):
    tag = f" [{mode}]" if SHOW_MODE_TAG else ""
    txt = (
        f"🤖 <b>{SELF_LABEL_IA}</b>{tag}\n"
        f"🎯 Número seco (G0): <b>{best}</b>\n"
        f"📈 Conf: <b>{conf*100:.2f}%</b> | Amostra≈<b>{tail_len}</b>"
    )
    await tg_broadcast(txt)

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
    if len(tail) < 2:
        return
    for t in range(1, len(tail)):
        nxt = tail[t]
        dist = (len(tail)-1) - t
        w = (decay ** dist)
        for n in range(2, max_n+1):
            if t-(n-1) < 0:
                break
            ctx = tail[t-(n-1):t]
            ctx_key = ",".join(str(x) for x in ctx)
            exec_write("""
                INSERT INTO ngram_stats (n, ctx, next, weight)
                VALUES (?,?,?,?)
                ON CONFLICT(n, ctx, next) DO UPDATE SET weight = weight + excluded.weight
            """, (n, ctx_key, int(nxt), float(w)))

def prob_from_ngrams(ctx: List[int], candidate: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5:
        return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    row = query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key))
    tot = (row["w"] or 0.0) if row else 0.0
    if tot <= 0:
        return 0.0
    row2 = query_one(
        "SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?",
        (n, ctx_key, candidate)
    )
    w = (row2["weight"] or 0.0) if row2 else 0.0
    return w / tot

# =========================
# Parsers do canal
# =========================
MUST_HAVE = (r"ENTRADA\s+CONFIRMADA", r"Mesa:\s*Fantan\s*-\s*Evolution")
MUST_NOT  = (r"\bANALISANDO\b", r"\bPlacar do dia\b", r"\bAPOSTA ENCERRADA\b")

def is_real_entry(text: str) -> bool:
    t = re.sub(r"\s+", " ", text).strip()
    for bad in MUST_NOT:
        if re.search(bad, t, flags=re.I):
            return False
    for good in MUST_HAVE:
        if not re.search(good, t, flags=re.I):
            return False
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
        if len(base) == k:
            break
    return base

def parse_bases_and_pattern(text: str) -> Tuple[List[int], str]:
    t = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"\bKWOK\s*([1-4])\s*-\s*([1-4])", t, flags=re.I)
    if m:
        a,b = int(m.group(1)), int(m.group(2))
        return [a,b], f"KWOK-{a}-{b}"
    if re.search(r"\bODD\b", t, flags=re.I):
        return [1,3], "ODD"
    if re.search(r"\bEVEN\b", t, flags=re.I):
        return [2,4], "EVEN"
    m = re.search(r"\bSS?H\s*([1-4])(?:-([1-4]))?(?:-([1-4]))?(?:-([1-4]))?", t, flags=re.I)
    if m:
        nums = [int(g) for g in m.groups() if g]
        return nums, "SSH-" + "-".join(str(x) for x in nums)
    m = re.search(r"Sequ[eê]ncia:\s*([\d\s\|\-]+)", t, flags=re.I)
    if m:
        parts = re.findall(r"[1-4]", m.group(1))
        base = bases_from_sequence_left_recent([int(x) for x in parts], 3)
        if base:
            return base, "SEQ"
    return [], "GEN"

# Detecao robusta de GREEN/RED
GREEN_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bGREEN\b.*?\((\d)\)", re.I | re.S),
    re.compile(r"\bGREEN\b.*?Número[:\s]*([1-4])", re.I | re.S),
    re.compile(r"\bGREEN\b.*?\(([1-4])\)", re.I | re.S),
]
RED_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\((.*?)\)", re.I | re.S),
    re.compile(r"\bLOSS\b.*?Número[:\s]*([1-4])", re.I | re.S),
    re.compile(r"\bRED\b.*?\(([1-4])\)", re.I | re.S),
]

def extract_green_number(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in GREEN_PATTERNS:
        m = rx.search(t)
        if m:
            nums = re.findall(r"[1-4]", m.group(1))
            if nums:
                return int(nums[0])
    return None

def extract_red_last_left(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in RED_PATTERNS:
        m = rx.search(t)
        if m:
            nums = re.findall(r"[1-4]", m.group(1))
            if nums:
                return int(nums[0])
    return None

def is_analise(text:str) -> bool:
    return bool(re.search(r"\bANALISANDO\b", text, flags=re.I))

# =========================
# Estatisticas & placar diario
# =========================
def laplace_ratio(wins:int, losses:int) -> float:
    return (wins + 1.0) / (wins + losses + 2.0)

def bump_pattern(pattern_key: str, number: int, won: bool):
    row = query_one("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pattern_key, number))
    w = row["wins"] if row else 0
    l = row["losses"] if row else 0
    if won: w += 1
    else:   l += 1
    exec_write("""
      INSERT INTO stats_pattern (pattern_key, number, wins, losses)
      VALUES (?,?,?,?)
      ON CONFLICT(pattern_key, number)
      DO UPDATE SET wins=excluded.wins, losses=excluded.losses
    """, (pattern_key, number, w, l))

def bump_strategy(strategy: str, number: int, won: bool):
    row = query_one("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?", (strategy, number))
    w = row["wins"] if row else 0
    l = row["losses"] if row else 0
    if won: w += 1
    else:   l += 1
    exec_write("""
      INSERT INTO stats_strategy (strategy, number, wins, losses)
      VALUES (?,?,?,?)
      ON CONFLICT(strategy, number)
      DO UPDATE SET wins=excluded.wins, losses=excluded.losses
    """, (strategy, number, w, l))

def today_key_local() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")

def update_daily_score(stage: Optional[int], won: bool):
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    if not row:
        g0=g1=g2=loss=streak=0
    else:
        g0,g1,g2,loss,streak = row["g0"],row["g1"],row["g2"],row["loss"],row["streak"]

    if SCORE_G0_ONLY:
        if won and stage == 0:
            g0 += 1
            streak += 1
        elif not won:
            loss += 1
            streak = 0
    else:
        if won:
            if stage == 0: g0 += 1
            elif stage == 1: g1 += 1
            elif stage == 2: g2 += 1
            streak += 1
        else:
            loss += 1
            streak = 0

    exec_write("""
      INSERT INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
      VALUES (?,?,?,?,?,?)
      ON CONFLICT(yyyymmdd) DO UPDATE SET
        g0=excluded.g0, g1=excluded.g1, g2=excluded.g2, loss=excluded.loss, streak=excluded.streak
    """, (y,g0,g1,g2,loss,streak))

    if SCORE_G0_ONLY:
        total = g0 + loss
        acc = (g0/total) if total else 0.0
        return g0, loss, acc, streak
    else:
        total = g0 + g1 + g2 + loss
        acc = ((g0+g1+g2)/total) if total else 0.0
        return g0, g1, g2, loss, acc, streak

async def send_scoreboard():
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0=row["g0"] if row else 0
    g1=row["g1"] if row else 0
    g2=row["g2"] if row else 0
    loss=row["loss"] if row else 0
    streak=row["streak"] if row else 0

    if SCORE_G0_ONLY:
        total = g0 + loss
        acc = (g0/total*100) if total else 0.0
        txt = (f"📊 <b>Placar do dia</b>\n"
               f"🟢 G0:{g0}  🔴 Loss:{loss}\n"
               f"✅ Acerto: {acc:.2f}%\n"
               f"🔥 Streak: {streak} GREEN(s)")
    else:
        total = g0 + g1 + g2 + loss
        acc = ((g0+g1+g2)/total*100) if total else 0.0
        txt = (f"📊 <b>Placar do dia</b>\n"
               f"🟢 G0:{g0} | G1:{g1} | G2:{g2}  🔴 Loss:{loss}\n"
               f"✅ Acerto: {acc:.2f}%\n"
               f"🔥 Streak: {streak} GREEN(s)")
    await tg_broadcast(txt)

# ======== Placar exclusivo IA ========
def update_daily_score_ia(stage: Optional[int], won: bool):
    y = today_key_local()
    row = query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    if not row:
        g0=loss=streak=0
    else:
        g0,loss,streak = row["g0"],row["loss"],row["streak"]

    if won and stage == 0:
        g0 += 1
        streak += 1
    elif not won:
        loss += 1
        streak = 0

    exec_write("""
      INSERT INTO daily_score_ia (yyyymmdd,g0,loss,streak)
      VALUES (?,?,?,?)
      ON CONFLICT(yyyymmdd) DO UPDATE SET
        g0=excluded.g0, loss=excluded.loss, streak=excluded.streak
    """, (y,g0,loss,streak))

    total = g0 + loss
    acc = (g0/total) if total else 0.0
    return g0, loss, acc, streak

def _convert_last_loss_to_green_ia():
    y = today_key_local()
    row = query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    if not row:
        exec_write("""INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak)
                      VALUES (?,0,0,0)""", (y,))
        return
    g0 = row["g0"] or 0
    loss = row["loss"] or 0
    streak = row["streak"] or 0
    if loss > 0:
        loss -= 1
        g0   += 1
        streak += 1
        exec_write("""
          INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak)
          VALUES (?,?,?,?)
        """, (y, g0, loss, streak))

async def send_scoreboard_ia():
    y = today_key_local()
    row = query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    g0=row["g0"] if row else 0
    loss=row["loss"] if row else 0
    streak=row["streak"] if row else 0
    total = g0 + loss
    acc = (g0/total*100) if total else 0.0
    txt = (f"🤖 <b>Placar IA (dia)</b>\n"
           f"🟢 G0:{g0}  🔴 Loss:{loss}\n"
           f"✅ Acerto: {acc:.2f}%\n"
           f"🔥 Streak: {streak} GREEN(s)")
    await tg_broadcast(txt)

# ======== Estatistica de recuperacao G1 ========
def bump_recov_g1(number:int, won:bool):
    row = query_one("SELECT wins, losses FROM recov_g1_stats WHERE number=?", (int(number),))
    w = (row["wins"] if row else 0) + (1 if won else 0)
    l = (row["losses"] if row else 0) + (0 if won else 1)
    exec_write("""
      INSERT INTO recov_g1_stats (number, wins, losses)
      VALUES (?,?,?)
      ON CONFLICT(number) DO UPDATE SET wins=excluded.wins, losses=excluded.losses
    """, (int(number), w, l))

def recov_g1_rate(number:int) -> Tuple[float,int]:
    row = query_one("SELECT wins, losses FROM recov_g1_stats WHERE number=?", (int(number),))
    if not row: return 0.0, 0
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    total = wins + losses
    rate = wins / total if total > 0 else 0.0
    return rate, total

# =========================
# Health (30 min) + Reset diario 00:00 UTC
# =========================
def _fmt_bytes(n: int) -> str:
    try:
        n = float(n)
    except Exception:
        return "—"
    for unit in ["B","KB","MB","GB","TB","PB"]:
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} EB"

def _dir_size_bytes(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except Exception:
                    pass
    except Exception:
        return 0
    return total

def _get_scalar(sql: str, params: tuple = (), default: int|float = 0):
    row = query_one(sql, params)
    if not row:
        return default
    try:
        return row[0] if row[0] is not None else default
    except Exception:
        keys = row.keys()
        return row[keys[0]] if keys and row[keys[0]] is not None else default

def _daily_score_snapshot():
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    if not row:
        return 0,0,0,0,0.0
    g0 = row["g0"] or 0
    loss = row["loss"] or 0
    streak = row["streak"] or 0
    total = g0 + loss
    acc = (g0/total*100.0) if total else 0.0
    return g0, loss, streak, total, acc

def _daily_score_snapshot_ia():
    y = today_key_local()
    row = query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    g0 = row["g0"] if row else 0
    loss = row["loss"] if row else 0
    total = g0 + loss
    acc = (g0/total*100.0) if total else 0.0
    return g0, loss, total, acc

def _last_snapshot_info():
    try:
        latest_path = os.path.join(INTEL_DIR, "snapshots", "latest_top.json")
        if not os.path.exists(latest_path):
            return "—"
        ts = os.path.getmtime(latest_path)
        return datetime.utcfromtimestamp(ts).replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return "—"

def _health_text() -> str:
    timeline_cnt   = _get_scalar("SELECT COUNT(*) FROM timeline")
    ngram_rows     = _get_scalar("SELECT COUNT(*) FROM ngram_stats")
    ngram_samples  = _get_scalar("SELECT SUM(weight) FROM ngram_stats")
    pat_rows       = _get_scalar("SELECT COUNT(*) FROM stats_pattern")
    pat_events     = _get_scalar("SELECT SUM(wins+losses) FROM stats_pattern")
    strat_rows     = _get_scalar("SELECT COUNT(*) FROM stats_strategy")
    strat_events   = _get_scalar("SELECT SUM(wins+losses) FROM stats_strategy")
    pend_open      = _get_scalar("SELECT COUNT(*) FROM pending_outcome WHERE open=1")
    intel_size     = _dir_size_bytes(INTEL_DIR)
    last_snap_ts   = _last_snapshot_info()
    g0, loss, streak, total, acc = _daily_score_snapshot()

    ia_g0, ia_loss, ia_total, ia_acc = _daily_score_snapshot_ia()

    return (
        "🩺 <b>Saude do Guardiao</b>\n"
        f"⏱️ UTC: <code>{utc_iso()}</code>\n"
        "—\n"
        f"🗄️ timeline: <b>{timeline_cnt}</b>\n"
        f"📚 ngram_stats: <b>{ngram_rows}</b> | amostras≈<b>{int(ngram_samples or 0)}</b>\n"
        f"🧩 stats_pattern: chaves=<b>{pat_rows}</b> | eventos=<b>{int(pat_events or 0)}</b>\n"
        f"🧠 stats_strategy: chaves=<b>{strat_rows}</b> | eventos=<b>{int(strat_events or 0)}</b>\n"
        f"⏳ pendencias abertas: <b>{pend_open}</b>\n"
        f"💾 INTEL dir: <b>{_fmt_bytes(intel_size)}</b> | ultimo snapshot: <code>{last_snap_ts}</code>\n"
        "—\n"
        f"📊 Placar (hoje - G0 only): G0=<b>{g0}</b> | Loss=<b>{loss}</b> | Total=<b>{total}</b>\n"
        f"✅ Acerto: <b>{acc:.2f}%</b> | 🔥 Streak: <b>{streak}</b>\n"
        "—\n"
        f"🤖 <b>IA</b> G0=<b>{ia_g0}</b> | Loss=<b>{ia_loss}</b> | Total=<b>{ia_total}</b>\n"
        f"✅ <b>IA Acerto (dia):</b> <b>{ia_acc:.2f}%</b>\n"
    )

async def _health_reporter_task():
    while True:
        try:
            await tg_broadcast(_health_text())
        except Exception as e:
            print(f"[HEALTH] erro ao enviar relatorio: {e}")
        await asyncio.sleep(30 * 60)

async def _daily_reset_task():
    while True:
        try:
            now = datetime.now(timezone.utc)
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait = max(1.0, (tomorrow - now).total_seconds())
            await asyncio.sleep(wait)

            y = today_key_local()
            exec_write("""
              INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
              VALUES (?,0,0,0,0,0)
            """, (y,))
            exec_write("""
              INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak)
              VALUES (?,0,0,0)
            """, (y,))
            await tg_broadcast("🕛 <b>Reset diario executado (00:00 UTC)</b>\n📊 Placar geral e IA zerados.")
        except Exception as e:
            print(f"[RESET] erro: {e}")
            await asyncio.sleep(60)

# =========================
# Pendencias (FastResult)
# =========================
def open_pending(strategy: Optional[str], suggested: int, source: str = "CHAN"):
    exec_write("""
        INSERT INTO pending_outcome
        (created_at,strategy,suggested,stage,open,window_left,seen_numbers,announced,source)
        VALUES (?,?,?,?,1,?, '', 0, ?)
    """, (now_ts(), strategy or "", int(suggested), 0, MAX_STAGE, source))
    try:
        INTEL.start_signal(suggested=int(suggested), strategy=strategy)
    except Exception as e:
        print(f"[INTEL] start_signal error: {e}")

def _convert_last_loss_to_green():
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    if not row:
        exec_write("""INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
                      VALUES (?,0,0,0,0,0)""", (y,))
        return
    g0 = row["g0"] or 0
    loss = row["loss"] or 0
    streak = row["streak"] or 0
    if loss > 0:
        loss -= 1
        g0   += 1
        streak += 1
        exec_write("""
          INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
          VALUES (?,?,?,?,?,?)
        """, (y, g0, row["g1"] or 0, row["g2"] or 0, loss, streak))

# ===== Helpers IA2 (antispam + cooldown) =====
def _ia2_hour_key() -> int:
    return int(datetime.now(timezone.utc).strftime("%Y%m%d%H"))

def _ia2_reset_hour():
    global _ia2_sent_this_hour, _ia2_hour_bucket
    hb = _ia2_hour_key()
    if _ia2_hour_bucket != hb:
        _ia2_hour_bucket = hb
        _ia2_sent_this_hour = 0

def _ia2_antispam_ok() -> bool:
    _ia2_reset_hour()
    return _ia2_sent_this_hour < IA2_MAX_PER_HOUR

def _ia2_mark_sent():
    global _ia2_sent_this_hour
    _ia2_sent_this_hour += 1

def _ia2_blocked_now() -> bool:
    return now_ts() < _ia2_blocked_until_ts

def _ia_set_post_loss_block():
    global _ia2_blocked_until_ts
    _ia2_blocked_until_ts = now_ts() + int(IA2_COOLDOWN_AFTER_LOSS)

async def close_pending_with_result(n_real: int, event_kind: str):
    rows = query_all("""
        SELECT id,strategy,suggested,stage,open,window_left,seen_numbers,announced,source
        FROM pending_outcome
        WHERE open=1
        ORDER BY id
    """)
    for r in rows:
        pid   = int(r["id"])
        strat = (r["strategy"] or "")
        sug   = int(r["suggested"])
        stage = int(r["stage"])
        left  = int(r["window_left"])
        announced = int(r["announced"])
        source = (r["source"] or "CHAN").upper()

        seen_txt = (r["seen_numbers"] or "")
        seen_txt2 = (seen_txt + ("|" if seen_txt else "") + str(n_real))
        exec_write("UPDATE pending_outcome SET seen_numbers=? WHERE id=?", (seen_txt2, pid))

        hit = (n_real == sug)

        if announced == 0:
            # Primeira janela (G0)
            if hit:
                bump_pattern("PEND", sug, True)
                if strat: bump_strategy(strat, sug, True)
                update_daily_score(0, True)
                if source == "IA":
                    update_daily_score_ia(0, True)
                await send_green_imediato(sug, "G0")
                exec_write("UPDATE pending_outcome SET announced=1, open=0 WHERE id=?", (pid,))
                await send_scoreboard()
                if source == "IA":
                    await send_scoreboard_ia()
            else:
                bump_pattern("PEND", sug, False)
                if strat: bump_strategy(strat, sug, False)
                update_daily_score(None, False)
                if source == "IA":
                    update_daily_score_ia(None, False)
                await send_loss_imediato(sug, "G0")

                # bloqueio pos-loss (IA)
                if source == "IA":
                    _ia_set_post_loss_block()

                exec_write("UPDATE pending_outcome SET announced=1 WHERE id=?", (pid,))
                await send_scoreboard()
                if source == "IA":
                    await send_scoreboard_ia()

                left -= 1
                if left <= 0 or MAX_STAGE <= 1:
                    exec_write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
                else:
                    next_stage = min(stage+1, MAX_STAGE-1)
                    exec_write(
                        "UPDATE pending_outcome SET window_left=?, stage=? WHERE id=?",
                        (left, next_stage, pid)
                    )
        else:
            # Recuperacao (G1/G2)
            left -= 1
            if hit:
                stxt = f"G{stage}"
                await send_recovery(stxt)

                try:
                    _convert_last_loss_to_green()
                    if source == "IA":
                        _convert_last_loss_to_green_ia()
                    await send_scoreboard()
                    if source == "IA":
                        await send_scoreboard_ia()
                except Exception as _e:
                    print(f"[SCORE] convert loss->green erro: {_e}")

                if stage == 1:
                    try:
                        bump_recov_g1(sug, True)
                    except Exception as _e:
                        print(f"[RECOV_G1] erro mark win: {_e}")

                bump_pattern("RECOV", sug, True)
                if strat: bump_strategy(strat, sug, True)
                exec_write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
            else:
                if left <= 0:
                    if stage >= 1:
                        try:
                            bump_recov_g1(sug, False)
                        except Exception as _e:
                            print(f"[RECOV_G1] erro mark loss: {_e}")
                    exec_write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
                else:
                    next_stage = min(stage+1, MAX_STAGE-1)
                    exec_write(
                        "UPDATE pending_outcome SET window_left=?, stage=? WHERE id=?",
                        (left, next_stage, pid)
                    )

    try:
        still_open = query_one("SELECT 1 FROM pending_outcome WHERE open=1")
        if not still_open:
            INTEL.stop_signal()
    except Exception as e:
        print(f"[INTEL] stop_signal check error: {e}")

# =========================
# Predicao (numero seco)
# =========================
def ngram_backoff_score(tail: List[int], after_num: Optional[int], candidate: int) -> float:
    score = 0.0
    if not tail:
        return 0.0
    if after_num is not None:
        idxs = [i for i,v in enumerate(tail) if v == after_num]
        if not idxs:
            ctx4 = tail[-4:] if len(tail) >= 4 else []
            ctx3 = tail[-3:] if len(tail) >= 3 else []
            ctx2 = tail[-2:] if len(tail) >= 2 else []
            ctx1 = tail[-1:] if len(tail) >= 1 else []
        else:
            i = idxs[-1]
            ctx1 = tail[max(0,i):i+1]
            ctx2 = tail[max(0,i-1):i+1] if i-1>=0 else []
            ctx3 = tail[max(0,i-2):i+1] if i-2>=0 else []
            ctx4 = tail[max(0,i-3):i+1] if i-3>=0 else []
    else:
        ctx4 = tail[-4:] if len(tail) >= 4 else []
        ctx3 = tail[-3:] if len(tail) >= 3 else []
        ctx2 = tail[-2:] if len(tail) >= 2 else []
        ctx1 = tail[-1:] if len(tail) >= 1 else []

    parts = []
    if len(ctx4)==4: parts.append((W4, prob_from_ngrams(ctx4[:-1], candidate)))
    if len(ctx3)==3: parts.append((W3, prob_from_ngrams(ctx3[:-1], candidate)))
    if len(ctx2)==2: parts.append((W2, prob_from_ngrams(ctx2[:-1], candidate)))
    if len(ctx1)==1: parts.append((W1, prob_from_ngrams(ctx1[:-1], candidate)))
    for w,p in parts:
        score += w*p
    return score

def confident_best(post: Dict[int,float], gap: float = GAP_MIN) -> Optional[int]:
    a = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a:
        return None
    if len(a)==1:
        return a[0][0]
    return a[0][0] if (a[0][1]-a[1][1]) >= gap else None

# suggest_number com prior off-base
def suggest_number(base: List[int], pattern_key: str, strategy: Optional[str], after_num: Optional[int]) -> Tuple[Optional[int],float,int,Dict[int,float]]:
    if not base:
        base = [1,2,3,4]
        pattern_key = "GEN"

    hour_block = int(datetime.now(timezone.utc).hour // 2)
    pat_key = f"{pattern_key}|h{hour_block}"  # reservado para futuros usos

    tail = get_recent_tail(WINDOW)
    scores: Dict[int, float] = {}

    if after_num is not None:
        try:
            last_idx = max([i for i, v in enumerate(tail) if v == after_num])
        except ValueError:
            return None, 0.0, len(tail), {k: 1/len(base) for k in base}
        if (len(tail) - 1 - last_idx) > 60:
            return None, 0.0, len(tail), {k: 1/len(base) for k in base}

    base_set = set(base)
    candidates = [1,2,3,4]

    # prior: parte em base, parte off-base
    prior: Dict[int,float] = {}
    if ALLOW_OFFBASE:
        off_frac = OFFBASE_PRIOR_FRACTION
        on_frac  = 1.0 - off_frac
        on_prior  = on_frac  / max(1, len(base_set))
        off_prior = off_frac / max(1, 4 - len(base_set))
        for c in candidates:
            prior[c] = on_prior if c in base_set else off_prior
    else:
        for c in candidates:
            prior[c] = (1.0/len(base_set)) if c in base_set else 0.0

    # likelihood a partir dos n-grams (backoff padrao)
    for c in candidates:
        ctx4 = tail[-4:] if len(tail) >= 4 else []
        ctx3 = tail[-3:] if len(tail) >= 3 else []
        ctx2 = tail[-2:] if len(tail) >= 2 else []
        ctx1 = tail[-1:] if len(tail) >= 1 else []
        s = 0.0
        if len(ctx4)==4: s += W4 * prob_from_ngrams(ctx4[:-1], c)
        if len(ctx3)==3: s += W3 * prob_from_ngrams(ctx3[:-1], c)
        if len(ctx2)==2: s += W2 * prob_from_ngrams(ctx2[:-1], c)
        if len(ctx1)==1: s += W1 * prob_from_ngrams(ctx1[:-1], c)
        scores[c] = s

    # posterior proporcional a prior * score
    post: Dict[int,float] = {}
    for c in candidates:
        post[c] = max(1e-9, prior.get(c, 1e-9)) * max(1e-9, scores.get(c, 1e-9))

    # normaliza
    ssum = sum(post.values()) or 1.0
    for c in post:
        post[c] /= ssum

    best = max(post.items(), key=lambda kv: kv[1])[0] if post else None
    conf = post.get(best, 0.0) if best is not None else 0.0

    if len(tail) >= MIN_SAMPLES and best is not None:
        tops = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
        gap = (tops[0][1] - tops[1][1]) if len(tops) >= 2 else tops[0][1]
        if conf < MIN_CONF_G0 or gap < MIN_GAP_G0:
            return None, 0.0, len(tail), post

    return best, conf, len(tail), post

# =========================
# IA2 (G0-only no webhook)
# =========================
async def ia2_process_once():
    base, pattern_key, strategy, after_num = [], "GEN", None, None
    tail = get_recent_tail(WINDOW)
    best, conf_raw, tail_len, post = suggest_number(base, pattern_key, strategy, after_num)
    if best is None:
        return

    gap = 1.0
    if post and len(post) >= 2:
        top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
        if len(top) >= 2:
            gap = top[0][1] - top[1][1]

    fire = (conf_raw >= IA2_TIER_STRICT) or (conf_raw >= IA2_TIER_STRICT - IA2_DELTA_GAP and gap >= IA2_GAP_SAFETY)
    soft = (conf_raw >= IA2_TIER_SOFT)

    if fire and not _ia2_blocked_now() and _ia2_antispam_ok():
        open_pending(strategy, best, source="IA")  # IA dispara como G0
        await ia2_send_signal(best, conf_raw, tail_len, "FIRE")
        _ia2_mark_sent()
        return

    if soft:
        await tg_broadcast(
            f"🤖 <b>{SELF_LABEL_IA} [SOFT]</b>\n"
            f"• Sugestao: <b>{best}</b>\n"
            f"• Conf: <b>{conf_raw*100:.2f}%</b> | Gap: <b>{gap:.3f}</b> | Amostra≈<b>{tail_len}</b>\n"
            "ℹ️ Exploratória — sem janela."
        )
    else:
        await tg_broadcast(
            f"🤖 <b>{SELF_LABEL_IA} [INFO]</b>\n"
            f"• Candidato atual: <b>{best}</b>\n"
            f"• Conf: <b>{conf_raw*100:.2f}%</b> | Gap: <b>{gap:.3f}</b> | Amostra≈<b>{tail_len}</b>\n"
            "ℹ️ Coletando base — sem janela."
        )

async def _ia2_analyzer_task():
    while True:
        try:
            await ia2_process_once()
        except Exception as e:
            print(f"[IA2] analyzer error: {e}")
        await asyncio.sleep(INTEL_ANALYZE_INTERVAL)

# =========================
# INTEL (stub para snapshots)
# =========================
class _IntelStub:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(os.path.join(self.base_dir, "snapshots"), exist_ok=True)
        self._signal_active = False
        self._last_num = None

    def start_signal(self, suggested: int, strategy: Optional[str] = None):
        self._signal_active = True
        self._last_num = suggested
        try:
            path = os.path.join(self.base_dir, "snapshots", "latest_top.json")
            with open(path, "w") as f:
                json.dump({"ts": now_ts(), "suggested": suggested, "strategy": strategy}, f)
        except Exception as e:
            print(f"[INTEL] snapshot error: {e}")

    def stop_signal(self):
        self._signal_active = False
        self._last_num = None

INTEL = _IntelStub(INTEL_DIR)

# =========================
# WEBHOOK & Rotas
# =========================
@app.get("/")
async def ping():
    return {"ok": True, "ts": utc_iso()}

class TgUpdate(BaseModel):
    update_id: Optional[int] = None
    message: Optional[Dict[str, Any]] = None
    edited_message: Optional[Dict[str, Any]] = None
    channel_post: Optional[Dict[str, Any]] = None
    edited_channel_post: Optional[Dict[str, Any]] = None

def _extract_text(update: Dict[str, Any]) -> Tuple[Optional[str], Optional[int]]:
    for key in ["message", "edited_message", "channel_post", "edited_channel_post"]:
        msg = update.get(key)
        if msg and isinstance(msg, dict):
            text = msg.get("text") or msg.get("caption")
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            if text:
                return str(text), chat_id
    return None, None

@app.post("/webhook/{token}")
async def webhook(token: str, req: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        payload = await req.json()
    except Exception:
        payload = {}
    text, _chat_id = _extract_text(payload) if isinstance(payload, dict) else (None, None)

    if not text:
        # Mesmo sem texto, roda IA (nao trava)
        await ia2_process_once()
        return {"ok": True}

    t = text.strip()

    # Eventos de fechamento (GREEN/RED)
    n_green = extract_green_number(t)
    n_red_left = extract_red_last_left(t)
    if n_green is not None:
        await close_pending_with_result(n_green, "GREEN")
        update_ngrams()
        await ia2_process_once()
        return {"ok": True, "event": "green"}
    if n_red_left is not None:
        await close_pending_with_result(n_red_left, "RED")
        update_ngrams()
        await ia2_process_once()
        return {"ok": True, "event": "red"}

    # Entrada confirmada do canal
    if is_real_entry(t):
        strategy = extract_strategy(t)
        _seq_raw = extract_seq_raw(t)
        after_x = extract_after_num(t)
        base, pattern_key = parse_bases_and_pattern(t)
        sug, conf, tail_len, _post = suggest_number(base, pattern_key, strategy, after_x)
        if sug is not None:
            open_pending(strategy, sug, source="CHAN")
            await tg_broadcast(
                f"📥 <b>Entrada do canal</b>\n"
                f"🎯 Número sugerido: <b>{sug}</b>\n"
                f"🏷️ Padrão: <code>{pattern_key}</code> | Estratégia: <code>{strategy or '-'}</code>\n"
                f"📈 Conf: <b>{conf*100:.2f}%</b> | Cauda≈<b>{tail_len}</b>"
            )

    # IA G0-only roda mesmo em textos comuns
    await ia2_process_once()
    return {"ok": True}

# =========================
# STARTUP TASKS
# =========================
@app.on_event("startup")
async def _startup_tasks():
    asyncio.create_task(_health_reporter_task())
    asyncio.create_task(_daily_reset_task())
    asyncio.create_task(_ia2_analyzer_task())