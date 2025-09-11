# -*- coding: utf-8 -*-
# Fan Tan — Guardião (G0 + Recuperação) com:
# - Inteligência em disco: /var/data/ai_intel (teto 1 GB + rotação)
# - Batidas do sinal a cada 20s enquanto houver pendência aberta
# - Analisador periódico a cada 2s com top combos recentes da cauda
#
# Rotas:
#   POST /webhook/<WEBHOOK_TOKEN>   (recebe mensagens do Telegram)
#   GET  /                          (ping)
#
# ENV obrigatórias:
#   TG_BOT_TOKEN, PUBLIC_CHANNEL, WEBHOOK_TOKEN
#
# ENV opcionais:
#   DB_PATH (default: /data/data.db)
#   INTEL_DIR (default: /var/data/ai_intel)
#   INTEL_MAX_BYTES (default: 1000000000 -> 1 GB)
#   INTEL_SIGNAL_INTERVAL (default: 20)
#   INTEL_ANALYZE_INTERVAL (default: 2)
#
#   AUTO_FIRE=1                       -> habilita “Tiro seco por IA”
#   SELF_TH_G0=0.90                   -> (LEGADO) confiança mínima G0 para IA (será sobrescrita pelo auto-ramp)
#   SELF_REQUIRE_G1=1                 -> (LEGADO) exigir G1 100% no lookback (substituído por taxa mínima)
#   SELF_G1_LOOKBACK=50               -> (LEGADO) lookback (mantido p/ compatibilidade interna)
#   SELF_COOLDOWN_S=6                 -> cooldown de auto-disparo
#   SELF_LABEL_IA="Tiro seco por IA"  -> rótulo do auto-sinal
#
#   --- NOVO (Auto-Ramp & Debug) ---
#   IA_TH_G0_START=0.55               -> threshold inicial de G0 (55%)
#   IA_TH_G0_MAX=0.80                 -> teto de G0
#   IA_G1_REQ_START=0.75              -> taxa mínima de G1 exigida (75%)
#   IA_G1_REQ_MAX=0.90                -> teto para G1 exigida
#   IA_G1_MIN_EVENTS=20               -> mínimo de eventos de G1 para considerar taxa
#   IA_RAMP_STEP=0.01                 -> passo por ajuste
#   IA_RAMP_MIN_EVENTS=20             -> mínimo de sinais IA no dia para permitir ramp
#   IA_RAMP_UP_AT=0.62                -> se acc IA >= 62% e total>=min, sobe thresholds
#   IA_RAMP_DOWN_AT=0.52              -> se acc IA <= 52% e total>=min, desce thresholds
#   IA_DEBUG=0                        -> 1 para logar ajustes/quase-disparos no canal de réplica
#
#   --- NOVO (Off-base Prior) ---
#   ALLOW_OFFBASE=1                   -> avalia candidatos 1..4 mesmo fora da base do padrão
#   OFFBASE_PRIOR_FRACTION=0.25       -> fração do prior reservada ao off-base (0.0–0.9)
#
#   --- NOVO (Explore / Risco / Rotulagem) ---
#   EXPLORE_SIGNALS=5                 -> no começo do dia, emitir até N sinais “explicativos”
#   MIN_SAMPLES_FOR_SIGNAL=30         -> amostra mínima de padrão p/ EXPLORE
#   MAX_SIGNALS_PER_HOUR=6            -> antispam (IA)
#   DAILY_STOP_LOSS=5                 -> parar IA após N losses no dia (G0 only)
#   COOLDOWN_AFTER_LOSS=60            -> segurar IA por N segundos após um loss
#   SHOW_MODE_TAG=1                   -> exibe [EXPLORE]/[STRICT] no texto do auto-sinal

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

# --- Migração automática do DB antigo (preserva aprendizado) ---
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
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()   # -100... ou @canal (apenas leitura)
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Canal de réplica (fixo)
REPL_ENABLED  = True
REPL_CHANNEL  = "-1003052132833"  # @fantanautomatico2

# =========================
# MODO: Resultado rápido com recuperação
# =========================
MAX_STAGE    = 3         # 3 = G0, G1, G2 (recuperação)
FAST_RESULT  = True      # publica G0 imediatamente; G1/G2 só informam "Recuperou" se bater
SCORE_G0_ONLY= True      # placar do dia só conta G0 e Loss (limpo)

# =========================
# Hiperparâmetros (ajuste G0 equilibrado)
# =========================
WINDOW = 400
DECAY  = 0.985
# Pesos (menos overfit em contexto longo)
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12
ALPHA, BETA, GAMMA = 1.05, 0.70, 0.40
GAP_MIN = 0.08

# Filtros de qualidade para G0 (modo equilibrado)
MIN_CONF_G0 = 0.55      # confiança mínima do top1
MIN_GAP_G0  = 0.04      # distância top1 - top2
MIN_SAMPLES = 20000     # só aplica filtros rígidos quando já há amostra suficiente

# =========================
# Inteligência em disco (/var/data) — ENV
# =========================
INTEL_DIR = os.getenv("INTEL_DIR", "/var/data/ai_intel").rstrip("/")
INTEL_MAX_BYTES = int(os.getenv("INTEL_MAX_BYTES", "1000000000"))  # 1 GB
INTEL_SIGNAL_INTERVAL = float(os.getenv("INTEL_SIGNAL_INTERVAL", "20"))  # beat do sinal
INTEL_ANALYZE_INTERVAL = float(os.getenv("INTEL_ANALYZE_INTERVAL", "2"))  # analisador

# =========================
# Auto Sinal (com Auto-Ramp)
# =========================
AUTO_FIRE          = (os.getenv("AUTO_FIRE", "1").strip() == "1")
SELF_TH_G0         = float(os.getenv("SELF_TH_G0", "0.90"))   # legado (não usado diretamente)
SELF_REQUIRE_G1    = (os.getenv("SELF_REQUIRE_G1", "1").strip() == "1")  # legado
SELF_G1_LOOKBACK   = int(os.getenv("SELF_G1_LOOKBACK", "50"))            # legado
SELF_COOLDOWN_S    = float(os.getenv("SELF_COOLDOWN_S", "6"))
SELF_LABEL_IA      = os.getenv("SELF_LABEL_IA", "Tiro seco por IA")

# --- NOVOS parâmetros de auto-ramp/limiares dinâmicos ---
IA_TH_G0_START     = float(os.getenv("IA_TH_G0_START", "0.55"))
IA_TH_G0_MAX       = float(os.getenv("IA_TH_G0_MAX", "0.80"))
IA_G1_REQ_START    = float(os.getenv("IA_G1_REQ_START", "0.75"))
IA_G1_REQ_MAX      = float(os.getenv("IA_G1_REQ_MAX", "0.90"))
IA_G1_MIN_EVENTS   = int(os.getenv("IA_G1_MIN_EVENTS", "20"))
IA_RAMP_STEP       = float(os.getenv("IA_RAMP_STEP", "0.01"))
IA_RAMP_MIN_EVENTS = int(os.getenv("IA_RAMP_MIN_EVENTS", "20"))
IA_RAMP_UP_AT      = float(os.getenv("IA_RAMP_UP_AT", "0.62"))
IA_RAMP_DOWN_AT    = float(os.getenv("IA_RAMP_DOWN_AT", "0.52"))
IA_DEBUG           = (os.getenv("IA_DEBUG", "0").strip() == "1")

# --- NOVO: Explore / Risco / Rotulagem ---
EXPLORE_SIGNALS        = int(os.getenv("EXPLORE_SIGNALS", "5"))
MIN_SAMPLES_FOR_SIGNAL = int(os.getenv("MIN_SAMPLES_FOR_SIGNAL", "30"))
MAX_SIGNALS_PER_HOUR   = int(os.getenv("MAX_SIGNALS_PER_HOUR", "6"))
DAILY_STOP_LOSS        = int(os.getenv("DAILY_STOP_LOSS", "5"))
COOLDOWN_AFTER_LOSS    = float(os.getenv("COOLDOWN_AFTER_LOSS", "60"))  # segundos
SHOW_MODE_TAG          = (os.getenv("SHOW_MODE_TAG", "1").strip() == "1")

# --- NOVO: Off-base Prior ---
ALLOW_OFFBASE = (os.getenv("ALLOW_OFFBASE", "1").strip() == "1")
OFFBASE_PRIOR_FRACTION = float(os.getenv("OFFBASE_PRIOR_FRACTION", "0.25"))

app = FastAPI(title="Fantan Guardião — FastResult (G0 + Recuperação G1/G2)", version="3.5.0")

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
    raise sqlite3.OperationalError("Banco bloqueado após várias tentativas.")

def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
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
# Init DB + migrações
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
    # Estatística de recuperação em G1
    cur.execute("""CREATE TABLE IF NOT EXISTS recov_g1_stats (
        number INTEGER PRIMARY KEY,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0
    )""")
    # --- NOVO: parâmetros dinâmicos da IA (auto-ramp) ---
    cur.execute("""CREATE TABLE IF NOT EXISTS ia_params (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    con.commit()
    # Migrações idempotentes
    try: cur.execute("ALTER TABLE pending_outcome ADD COLUMN seen_numbers TEXT DEFAULT ''"); con.commit()
    except sqlite3.OperationalError: pass
    try: cur.execute("ALTER TABLE pending_outcome ADD COLUMN announced INTEGER NOT NULL DEFAULT 0"); con.commit()
    except sqlite3.OperationalError: pass
    try: cur.execute("ALTER TABLE pending_outcome ADD COLUMN source TEXT NOT NULL DEFAULT 'CHAN'"); con.commit()
    except sqlite3.OperationalError: pass

    # Seed dos parâmetros dinâmicos (se não existirem)
    def _seed_param(k,v):
        cur.execute("INSERT OR IGNORE INTO ia_params (key,value) VALUES (?,?)", (k, str(v)))
    _seed_param("th_g0", IA_TH_G0_START)
    _seed_param("g1_req", IA_G1_REQ_START)
    _seed_param("ramp_step", IA_RAMP_STEP)
    _seed_param("g0_max", IA_TH_G0_MAX)
    _seed_param("g1_max", IA_G1_REQ_MAX)
    _seed_param("g1_min_events", IA_G1_MIN_EVENTS)
    _seed_param("ramp_min_events", IA_RAMP_MIN_EVENTS)
    _seed_param("ramp_up_at", IA_RAMP_UP_AT)
    _seed_param("ramp_down_at", IA_RAMP_DOWN_AT)
    con.commit()
    con.close()

init_db()

# Helpers para ia_params
def ia_get(key: str, default: Optional[float]=None) -> float:
    row = query_one("SELECT value FROM ia_params WHERE key=?", (key,))
    try:
        return float(row["value"]) if row else (default if default is not None else 0.0)
    except Exception:
        return default if default is not None else 0.0

def ia_set(key: str, value: float):
    exec_write("INSERT OR REPLACE INTO ia_params (key,value) VALUES (?,?)", (key, str(float(value))))

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
            json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True},
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
        nxt = tail[t]
        dist = (len(tail)-1) - t
        w = (decay ** dist)
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
    row = query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key))
    tot = (row["w"] or 0.0) if row else 0.0
    if tot <= 0: return 0.0
    row2 = query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, candidate))
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

# Detecção robusta de GREEN/RED (vários formatos)
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
            if nums: return int(nums[0])
    return None

def extract_red_last_left(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in RED_PATTERNS:
        m = rx.search(t)
        if m:
            nums = re.findall(r"[1-4]", m.group(1))
            if nums: return int(nums[0])
    return None

def is_analise(text:str) -> bool:
    return bool(re.search(r"\bANALISANDO\b", text, flags=re.I))

# =========================
# Estatísticas & placar diário
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

# Placar geral (já existente)
def update_daily_score(stage: Optional[int], won: bool):
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    if not row: g0=g1=g2=loss=streak=0
    else:       g0,g1,g2,loss,streak = row["g0"],row["g1"],row["g2"],row["loss"],row["streak"]

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
    if not row: g0=loss=streak=0
    else:       g0,loss,streak = row["g0"],row["loss"],row["streak"]

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

# ======== Estatística de recuperação G1 ========
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
# Health (30 min) + Reset diário 00:00 UTC
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

    # IA snapshot + thresholds dinâmicos
    ia_g0, ia_loss, ia_total, ia_acc = _daily_score_snapshot_ia()
    th_g0   = ia_get("th_g0", IA_TH_G0_START)
    g1_req  = ia_get("g1_req", IA_G1_REQ_START)

    return (
        "🩺 <b>Saúde do Guardião</b>\n"
        f"⏱️ UTC: <code>{utc_iso()}</code>\n"
        "—\n"
        f"🗄️ timeline: <b>{timeline_cnt}</b>\n"
        f"📚 ngram_stats: <b>{ngram_rows}</b> | amostras≈<b>{int(ngram_samples or 0)}</b>\n"
        f"🧩 stats_pattern: chaves=<b>{pat_rows}</b> | eventos=<b>{int(pat_events or 0)}</b>\n"
        f"🧠 stats_strategy: chaves=<b>{strat_rows}</b> | eventos=<b>{int(strat_events or 0)}</b>\n"
        f"⏳ pendências abertas: <b>{pend_open}</b>\n"
        f"💾 INTEL dir: <b>{_fmt_bytes(intel_size)}</b> | último snapshot: <code>{last_snap_ts}</code>\n"
        "—\n"
        f"📊 Placar (hoje - G0 only): G0=<b>{g0}</b> | Loss=<b>{loss}</b> | Total=<b>{total}</b>\n"
        f"✅ Acerto: <b>{acc:.2f}%</b> | 🔥 Streak: <b>{streak}</b>\n"
        "—\n"
        f"🤖 <b>IA</b> G0=<b>{ia_g0}</b> | Loss=<b>{ia_loss}</b> | Total=<b>{ia_total}</b>\n"
        f"✅ <b>IA Acerto (dia):</b> <b>{ia_acc:.2f}%</b>\n"
        f"⚙️ <b>IA thresholds:</b> G0≥<b>{th_g0*100:.1f}%</b> | G1≥<b>{g1_req*100:.1f}%</b>\n"
    )

async def _health_reporter_task():
    while True:
        try:
            await tg_broadcast(_health_text())
        except Exception as e:
            print(f"[HEALTH] erro ao enviar relatório: {e}")
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
            await tg_broadcast("🕛 <b>Reset diário executado (00:00 UTC)</b>\n📊 Placar geral e IA zerados para o novo dia.")
        except Exception as e:
            print(f"[RESET] erro: {e}")
            await asyncio.sleep(60)

# =========================
# Pendências (FastResult: G0 imediato + recuperação G1/G2)
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

# Conversão (geral) de 1 LOSS -> 1 GREEN ao recuperar
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

# Ajuste automático dos thresholds da IA (ramp up/down)
def _ia_auto_ramp_if_needed():
    # Usa o desempenho do dia (g0/loss) para ajustar
    y = today_key_local()
    row = query_one("SELECT g0,loss FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    g0 = row["g0"] if row else 0
    loss = row["loss"] if row else 0
    total = (g0 or 0) + (loss or 0)

    th_g0  = ia_get("th_g0", IA_TH_G0_START)
    g1_req = ia_get("g1_req", IA_G1_REQ_START)
    step   = ia_get("ramp_step", IA_RAMP_STEP)
    g0_max = ia_get("g0_max", IA_TH_G0_MAX)
    g1_max = ia_get("g1_max", IA_G1_REQ_MAX)
    min_ev = int(ia_get("ramp_min_events", IA_RAMP_MIN_EVENTS))
    up_at  = ia_get("ramp_up_at", IA_RAMP_UP_AT)
    dn_at  = ia_get("ramp_down_at", IA_RAMP_DOWN_AT)

    if total < min_ev or total <= 0:
        return  # ainda não ajusta

    acc = g0/total
    changed = False

    if acc >= up_at:
        new_th = min(g0_max, th_g0 + step)
        new_g1 = min(g1_max, g1_req + step/2.0)  # G1 sobe mais devagar
        if abs(new_th - th_g0) > 1e-9:
            ia_set("th_g0", new_th); th_g0 = new_th; changed = True
        if abs(new_g1 - g1_req) > 1e-9:
            ia_set("g1_req", new_g1); g1_req = new_g1; changed = True

    elif acc <= dn_at:
        new_th = max(IA_TH_G0_START, th_g0 - step)
        # G1 exigida não desce automaticamente para não “afrouxar” demais
        if abs(new_th - th_g0) > 1e-9:
            ia_set("th_g0", new_th); th_g0 = new_th; changed = True

    if IA_DEBUG and changed:
        asyncio.create_task(tg_broadcast(
            f"🛠️ <b>IA Auto-Ramp</b>\n"
            f"G0≥<b>{th_g0*100:.1f}%</b> | G1≥<b>{g1_req*100:.1f}%</b> (baseando-se em {total} sinais IA do dia)"
        ))

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
                    _ia_auto_ramp_if_needed()
            else:
                bump_pattern("PEND", sug, False)
                if strat: bump_strategy(strat, sug, False)
                update_daily_score(None, False)
                if source == "IA":
                    update_daily_score_ia(None, False)
                await send_loss_imediato(sug, "G0")

                # >>> BLOQUEIO pós-loss (IA)
                if source == "IA":
                    _ia_set_post_loss_block()

                exec_write("UPDATE pending_outcome SET announced=1 WHERE id=?", (pid,))
                await send_scoreboard()
                if source == "IA":
                    await send_scoreboard_ia()
                    _ia_auto_ramp_if_needed()

                left -= 1
                if left <= 0 or MAX_STAGE <= 1:
                    exec_write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
                else:
                    next_stage = min(stage+1, MAX_STAGE-1)
                    exec_write("UPDATE pending_outcome SET window_left=?, stage=? WHERE id=?", (left, next_stage, pid))
        else:
            # Recuperação (G1/G2)
            left -= 1
            if hit:
                stxt = f"G{stage}"
                await send_recovery(stxt)

                # converte último LOSS -> GREEN (geral e, se IA, no placar IA)
                try:
                    _convert_last_loss_to_green()
                    if source == "IA":
                        _convert_last_loss_to_green_ia()
                    await send_scoreboard()
                    if source == "IA":
                        await send_scoreboard_ia()
                        _ia_auto_ramp_if_needed()
                except Exception as _e:
                    print(f"[SCORE] convert loss->green erro: {_e}")

                # Marca estatística de recuperação por estágio
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
                    exec_write("UPDATE pending_outcome SET window_left=?, stage=? WHERE id=?", (left, next_stage, pid))

    try:
        still_open = query_one("SELECT 1 FROM pending_outcome WHERE open=1")
        if not still_open:
            INTEL.stop_signal()
    except Exception as e:
        print(f"[INTEL] stop_signal check error: {e}")

# =========================
# Predição (número seco)
# =========================
def ngram_backoff_score(tail: List[int], after_num: Optional[int], candidate: int) -> float:
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
    for w,p in parts: score += w*p
    return score

def confident_best(post: Dict[int,float], gap: float = GAP_MIN) -> Optional[int]:
    a = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a: return None
    if len(a)==1: return a[0][0]
    return a[0][0] if (a[0][1]-a[1][1]) >= gap else None

# ======= SUBSTITUÍDA: suggest_number com prior off-base =======
def suggest_number(base: List[int], pattern_key: str, strategy: Optional[str], after_num: Optional[int]) -> Tuple[Optional[int],float,int,Dict[int,float]]:
    # Base vazia -> universo completo
    if not base:
        base = [1,2,3,4]
        pattern_key = "GEN"

    hour_block = int(datetime.now(timezone.utc).hour // 2)
    pat_key = f"{pattern_key}|h{hour_block}"

    tail = get_recent_tail(WINDOW)
    scores: Dict[int, float] = {}

    # Regras “após X”: ignora se não tem lastro recente na cauda
    if after_num is not None:
        try:
            last_idx = max([i for i, v in enumerate(tail) if v == after_num])
        except ValueError:
            # sem lastro: abstém
            return None, 0.0, len(tail), {k: 1/len(base) for k in base}
        # se o "após X" estiver muito distante, abstém
        if (len(tail) - 1 - last_idx) > 60:
            return None, 0.0, len(tail), {k: 1/len(base) for k in base}

    # --- candidatos e prior com off-base ---
    base_set = set(base)
    candidates = [1,2,3