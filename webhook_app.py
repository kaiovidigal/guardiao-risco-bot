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

import os, re, json, time, sqlite3, asyncio, shutil
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime, timezone

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
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()   # -100... ou @canal
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()

if not TG_BOT_TOKEN or not PUBLIC_CHANNEL or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN, PUBLIC_CHANNEL e WEBHOOK_TOKEN.")

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

# Modelo / hiperparâmetros
WINDOW = 400
DECAY  = 0.985
W4, W3, W2, W1 = 0.45, 0.30, 0.17, 0.08
ALPHA, BETA, GAMMA = 1.10, 0.65, 0.35
GAP_MIN = 0.08

# =========================
# Inteligência em disco (/var/data) — ENV
# =========================
INTEL_DIR = os.getenv("INTEL_DIR", "/var/data/ai_intel").rstrip("/")
INTEL_MAX_BYTES = int(os.getenv("INTEL_MAX_BYTES", "1000000000"))  # 1 GB
INTEL_SIGNAL_INTERVAL = float(os.getenv("INTEL_SIGNAL_INTERVAL", "20"))  # beat do sinal
INTEL_ANALYZE_INTERVAL = float(os.getenv("INTEL_ANALYZE_INTERVAL", "2"))  # analisador

app = FastAPI(title="Fantan Guardião — FastResult (G0 + Recuperação G1/G2)", version="3.2.0")

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
        announced INTEGER NOT NULL DEFAULT 0
    )""")
    con.commit()
    try: cur.execute("ALTER TABLE pending_outcome ADD COLUMN seen_numbers TEXT DEFAULT ''"); con.commit()
    except sqlite3.OperationalError: pass
    try: cur.execute("ALTER TABLE pending_outcome ADD COLUMN announced INTEGER NOT NULL DEFAULT 0"); con.commit()
    except sqlite3.OperationalError: pass
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
            json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True},
        )

async def tg_broadcast(text: str, parse: str="HTML"):
    if PUBLIC_CHANNEL:
        await tg_send_text(PUBLIC_CHANNEL, text, parse)
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

GREEN_RE   = re.compile(r"APOSTA\s+ENCERRADA.*?GREEN.*?\((\d)\)", re.I | re.S)
RED_NUM_RE = re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\((.*?)\)", re.I | re.S)

def extract_green_number(text: str) -> Optional[int]:
    m = GREEN_RE.search(text);  return int(m.group(1)) if m else None

def extract_red_last_left(text: str) -> Optional[int]:
    m = RED_NUM_RE.search(text)
    if not m: return None
    nums = re.findall(r"[1-4]", m.group(1))
    return int(nums[0]) if nums else None

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

def update_daily_score(stage: Optional[int], won: bool):
    """
    Se SCORE_G0_ONLY=True: só conta G0 e Loss.
    Caso contrário, conta G0/G1/G2 separadamente.
    """
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

# =========================
# Inteligência em disco — Manager
# =========================
def _intel_ensure_dir():
    os.makedirs(INTEL_DIR, exist_ok=True)
    os.makedirs(os.path.join(INTEL_DIR, "signals"), exist_ok=True)
    os.makedirs(os.path.join(INTEL_DIR, "snapshots"), exist_ok=True)

def _dir_size_bytes(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total

def _rotate_if_needed():
    try:
        total = _dir_size_bytes(INTEL_DIR)
        if total <= INTEL_MAX_BYTES:
            return
        # remove os mais antigos primeiro
        all_files = []
        for root, _, files in os.walk(INTEL_DIR):
            for f in files:
                p = os.path.join(root, f)
                try:
                    all_files.append((p, os.path.getmtime(p)))
                except Exception:
                    pass
        all_files.sort(key=lambda x: x[1])  # antigos primeiro
        idx = 0
        while total > INTEL_MAX_BYTES and idx < len(all_files):
            p, _ = all_files[idx]
            try:
                sz = os.path.getsize(p)
                os.remove(p)
                total -= sz
            except Exception:
                pass
            idx += 1
    except Exception as e:
        print(f"[INTEL] rotate error: {e}")

class IntelManager:
    def __init__(self):
        self._active = False
        self._last_suggested: Optional[int] = None
        self._last_strategy: Optional[str] = None
        self._signal_task: Optional[asyncio.Task] = None
        self._analyze_task: Optional[asyncio.Task] = None
        _intel_ensure_dir()

    def has_open_signal(self) -> bool:
        return self._active

    def start_signal(self, suggested: int, strategy: Optional[str]):
        self._active = True
        self._last_suggested = int(suggested)
        self._last_strategy = (strategy or "")
        if self._signal_task is None or self._signal_task.done():
            self._signal_task = asyncio.create_task(self._signal_beats())
        if self._analyze_task is None or self._analyze_task.done():
            self._analyze_task = asyncio.create_task(self._periodic_analyzer())

    def stop_signal(self):
        self._active = False
        if self._signal_task and not self._signal_task.done():
            self._signal_task.cancel()
            self._signal_task = None

    async def _signal_beats(self):
        """Grava heartbeat do sinal a cada INTEL_SIGNAL_INTERVAL segundos enquanto ativo."""
        try:
            while self._active:
                payload = {
                    "ts": now_ts(),
                    "utc": utc_iso(),
                    "active": True,
                    "suggested": self._last_suggested,
                    "strategy": self._last_strategy,
                    "tail_sample": get_recent_tail(40),
                }
                day = today_key()
                fpath = os.path.join(INTEL_DIR, "signals", f"sinal_{day}.jsonl")
                with open(fpath, "a", encoding="utf-8") as fp:
                    fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
                _rotate_if_needed()
                await asyncio.sleep(max(0.2, INTEL_SIGNAL_INTERVAL))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[INTEL] signal_beats error: {e}")

    def _best_combos_from_tail(self) -> Dict[str, Any]:
        """Calcula melhores combinações recentes baseado na cauda (ngrams + stats)."""
        tail = get_recent_tail(WINDOW)
        if not tail:
            return {"ts": now_ts(), "utc": utc_iso(), "tail_len": 0, "top": []}

        candidates = [1, 2, 3, 4]
        ctx4 = tail[-4:] if len(tail) >= 4 else tail[:]
        ctx3 = tail[-3:] if len(tail) >= 3 else tail[:]
        ctx2 = tail[-2:] if len(tail) >= 2 else tail[:]
        ctx1 = tail[-1:] if len(tail) >= 1 else tail[:]

        scores: Dict[int, float] = {}
        for c in candidates:
            p = 0.0
            if len(ctx4) >= 1: p += W4 * prob_from_ngrams(ctx4[:-1], c)
            if len(ctx3) >= 1: p += W3 * prob_from_ngrams(ctx3[:-1], c)
            if len(ctx2) >= 1: p += W2 * prob_from_ngrams(ctx2[:-1], c)
            if len(ctx1) >= 1: p += W1 * prob_from_ngrams(ctx1[:-1], c)

            hour_block = int(datetime.now(timezone.utc).hour // 2)
            pat_key = f"GEN|h{hour_block}"
            rowp = query_one("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pat_key, c))
            pw = rowp["wins"] if rowp else 0
            pl = rowp["losses"] if rowp else 0
            p_pat = laplace_ratio(pw, pl)

            prior = 0.25
            score = (prior) * ((max(p, 1e-9)) ** ALPHA) * (p_pat ** BETA)
            scores[c] = score

        tot = sum(scores.values()) or 1e-9
        post = {k: v / tot for k, v in scores.items()}
        top_sorted = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
        top_list = [{"number": n, "prob": float(p)} for n, p in top_sorted]
        return {
            "ts": now_ts(),
            "utc": utc_iso(),
            "tail_len": len(tail),
            "top": top_list,
        }

    async def _periodic_analyzer(self):
        """A cada INTEL_ANALYZE_INTERVAL grava snapshot dos 'melhores números' recentes."""
        try:
            while True:
                snap = self._best_combos_from_tail()
                latest_path = os.path.join(INTEL_DIR, "snapshots", "latest_top.json")
                with open(latest_path, "w", encoding="utf-8") as fp:
                    json.dump(snap, fp, ensure_ascii=False, indent=2)
                hist_path = os.path.join(INTEL_DIR, "snapshots", "top_history.jsonl")
                with open(hist_path, "a", encoding="utf-8") as fp:
                    fp.write(json.dumps(snap, ensure_ascii=False) + "\n")
                _rotate_if_needed()
                await asyncio.sleep(max(0.2, INTEL_ANALYZE_INTERVAL))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[INTEL] analyzer error: {e}")

INTEL = IntelManager()

# =========================
# Pendências (FastResult: G0 imediato + recuperação G1/G2)
# =========================
def open_pending(strategy: Optional[str], suggested: int):
    exec_write("""
        INSERT INTO pending_outcome
        (created_at,strategy,suggested,stage,open,window_left,seen_numbers,announced)
        VALUES (?,?,?,?,1,?, '', 0)
    """, (now_ts(), strategy or "", int(suggested), 0, MAX_STAGE))
    # >>> Hook de inteligência: inicia batidas do sinal
    try:
        INTEL.start_signal(suggested=int(suggested), strategy=strategy)
    except Exception as e:
        print(f"[INTEL] start_signal error: {e}")

async def close_pending_with_result(n_real: int, event_kind: str):
    """
    FastResult:
      - No primeiro resultado (G0): publica GREEN/LOSS imediatamente (announced=1).
      - Mantém pendência aberta até G1/G2 (se MAX_STAGE>1).
    """
    rows = query_all("""
        SELECT id,strategy,suggested,stage,open,window_left,seen_numbers,announced
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

        # registra visto
        seen_txt = (r["seen_numbers"] or "")
        seen_txt2 = (seen_txt + ("|" if seen_txt else "") + str(n_real))
        exec_write("UPDATE pending_outcome SET seen_numbers=? WHERE id=?", (seen_txt2, pid))

        hit = (n_real == sug)

        if announced == 0:
            # PRIMEIRO resultado público (G0)
            if hit:
                bump_pattern("PEND", sug, True)
                if strat: bump_strategy(strat, sug, True)
                update_daily_score(0, True)
                await send_green_imediato(sug, "G0")
                exec_write("UPDATE pending_outcome SET announced=1, open=0 WHERE id=?", (pid,))
                await send_scoreboard()
            else:
                # LOSS imediato público (em G0)
                bump_pattern("PEND", sug, False)
                if strat: bump_strategy(strat, sug, False)
                update_daily_score(None, False)  # conta LOSS no placar (G0-only)
                await send_loss_imediato(sug, "G0")
                exec_write("UPDATE pending_outcome SET announced=1 WHERE id=?", (pid,))
                await send_scoreboard()

                # Se MAX_STAGE>1, seguimos tentando recuperação silenciosa
                left -= 1
                if left <= 0 or MAX_STAGE <= 1:
                    exec_write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
                else:
                    next_stage = min(stage+1, MAX_STAGE-1)
                    exec_write("UPDATE pending_outcome SET window_left=?, stage=? WHERE id=?", (left, next_stage, pid))
        else:
            # Já anunciamos G0; agora somente RECUPERAÇÃO (silenciosa)
            left -= 1
            if hit:
                stxt = f"G{stage}"
                await send_recovery(stxt)
                bump_pattern("RECOV", sug, True)
                if strat: bump_strategy(strat, sug, True)
                exec_write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
            else:
                if left <= 0:
                    exec_write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
                else:
                    next_stage = min(stage+1, MAX_STAGE-1)
                    exec_write("UPDATE pending_outcome SET window_left=?, stage=? WHERE id=?", (left, next_stage, pid))

    # >>> Hook de inteligência: para batidas se não houver mais pendências
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

def suggest_number(base: List[int], pattern_key: str, strategy: Optional[str], after_num: Optional[int]) -> Tuple[Optional[int],float,int,Dict[int,float]]:
    if not base: base = [1,2,3,4]
    hour_block = int(datetime.now(timezone.utc).hour // 2)
    pat_key = f"{pattern_key}|h{hour_block}"

    tail = get_recent_tail(WINDOW)
    scores: Dict[int, float] = {}

    for c in base:
        ng = ngram_backoff_score(tail, after_num, c)

        rowp = query_one("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pat_key, c))
        pw = rowp["wins"] if rowp else 0
        pl = rowp["losses"] if rowp else 0
        p_pat = laplace_ratio(pw, pl)

        p_str = 1/len(base)
        if strategy:
            rows = query_one("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?", (strategy, c))
            sw = rows["wins"] if rows else 0
            sl = rows["losses"] if rows else 0
            p_str = laplace_ratio(sw, sl)

        prior = 1.0/len(base)
        score = (prior) * ((ng or 1e-6) ** ALPHA) * (p_pat ** BETA) * (p_str ** GAMMA)
        scores[c] = score

    total = sum(scores.values()) or 1e-9
    post = {k: v/total for k,v in scores.items()}

    number = confident_best(post, gap=GAP_MIN)
    conf = post.get(number, 0.0) if number is not None else 0.0

    roww = query_one("SELECT SUM(weight) AS s FROM ngram_stats")
    samples = int((roww["s"] or 0) if roww else 0)
    return number, conf, samples, post

def build_suggestion_msg(number:int, base:List[int], pattern_key:str,
                         after_num:Optional[int], conf:float, samples:int, stage:str="G0") -> str:
    base_txt = ", ".join(str(x) for x in base) if base else "—"
    aft_txt = f" após {after_num}" if after_num else ""
    return (
        f"🎯 <b>Número seco ({stage}):</b> <b>{number}</b>\n"
        f"🧩 <b>Padrão:</b> {pattern_key}{aft_txt}\n"
        f"🧮 <b>Base:</b> [{base_txt}]\n"
        f"📊 Conf: {conf*100:.2f}% | Amostra≈{samples}"
    )

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
# Routes
# =========================
@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>"}

# Inicia o analisador mesmo sem sinal ativo (útil para snapshots contínuos)
@app.on_event("startup")
async def _boot_analyzer():
    try:
        if INTEL._analyze_task is None or INTEL._analyze_task.done():
            INTEL._analyze_task = asyncio.create_task(INTEL._periodic_analyzer())
    except Exception as e:
        print(f"[INTEL] startup analyzer error: {e}")

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg:
        return {"ok": True}

    # texto da mensagem
    text = (msg.get("text") or msg.get("caption") or "").strip()
    t = re.sub(r"\s+", " ", text)

    # 1) GREEN / RED — registra número real e fecha/atualiza pendências
    gnum = extract_green_number(t)
    redn = extract_red_last_left(t)
    if gnum is not None or redn is not None:
        n_observed = gnum if gnum is not None else redn
        event_kind = "GREEN" if gnum is not None else "RED"
        append_timeline(n_observed)
        update_ngrams()
        await close_pending_with_result(n_observed, event_kind)

        # aprendizado auxiliar por estratégia (idempotente)
        strat = extract_strategy(t) or ""
        row = query_one("SELECT suggested_number, pattern_key FROM last_by_strategy WHERE strategy=?", (strat,))
        if row and gnum is not None:
            suggested = int(row["suggested_number"])
            pat_key   = row["pattern_key"] or "GEN"
            won = (suggested == int(gnum))
            bump_pattern(pat_key, suggested, won)
            if strat: bump_strategy(strat, suggested, won)

        return {"ok": True, "observed": n_observed, "kind": event_kind}

    # 2) ANALISANDO — só alimenta timeline
    if is_analise(t):
        seq_raw = extract_seq_raw(t)
        if seq_raw:
            parts = re.findall(r"[1-4]", seq_raw)
            seq_left_recent = [int(x) for x in parts]
            seq_old_to_new  = seq_left_recent[::-1]
            for n in seq_old_to_new:
                append_timeline(n)
            update_ngrams()
        return {"ok": True, "analise": True}

    # 3) ENTRADA CONFIRMADA — sugerir 1 número seco e abrir pendência
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

    number, conf, samples, post = suggest_number(base, pattern_key, strategy, after_num)
    if number is None:
        number = max(post, key=post.get)

    exec_write("""
      INSERT OR REPLACE INTO suggestions
      (source_msg_id, strategy, seq_raw, context_key, pattern_key, base, suggested_number, stage, sent_at)
      VALUES (?,?,?,?,?,?,?,?,?)
    """, (source_msg_id, strategy, seq_raw, "CTX", pattern_key, json.dumps(base), int(number), "G0", now_ts()))
    exec_write("""
      INSERT OR REPLACE INTO last_by_strategy
      (strategy, source_msg_id, suggested_number, context_key, pattern_key, stage, created_at)
      VALUES (?,?,?,?,?,?,?)
    """, (strategy, source_msg_id, int(number), "CTX", pattern_key, "G0", now_ts()))

    open_pending(strategy, int(number))  # (hook INTEL chamado dentro)

    # ENTRADA — formato completo
    out = build_suggestion_msg(int(number), base, pattern_key, after_num, conf, samples, stage="G0")
    await tg_broadcast(out)
    return {"ok": True, "sent": True, "number": number, "conf": conf, "samples": samples}