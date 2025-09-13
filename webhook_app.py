# -*- coding: utf-8 -*-
"""
Fantan Guardião — FIRE-only (G0 + Recuperação oculta), versão enxuta
Ajustes solicitados:
- Enviar SOMENTE IA FIRE no canal secundário -1002796105884 (com "ok")
- Postar também o resultado (WIN/LOSS) da IA nesse canal (com "ok")
- Evitar misturar: broadcasts gerais (health/placar/entradas do canal) desativados
"""
import os, re, json, time, sqlite3, asyncio, shutil
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# ENV / CONFIG
# =========================
getenv = os.getenv
DB_PATH       = (getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db")
TG_BOT_TOKEN  = getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN = getenv("WEBHOOK_TOKEN", "").strip()
FLUSH_KEY     = getenv("FLUSH_KEY", "meusegredo123").strip()
TELEGRAM_API  = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Desabilita broadcasts gerais (não misturar)
REPL_ENABLED  = False
REPL_CHANNEL  = ""

# Canal exclusivo para IA FIRE
IA_FIRE_CHANNEL = "-1002796105884"

# Modo/estratégia
MAX_STAGE, FAST_RESULT, SCORE_G0_ONLY = 3, True, True

# Hiperparâmetros
WINDOW, DECAY = 400, 0.985
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12
ALPHA, BETA, GAMMA, GAP_MIN = 1.05, 0.70, 0.40, 0.08

MIN_CONF_G0, MIN_GAP_G0, MIN_SAMPLES = 0.55, 0.04, 1000

INTEL_DIR = getenv("INTEL_DIR", "/var/data/ai_intel").rstrip("/")
INTEL_SIGNAL_INTERVAL = float(getenv("INTEL_SIGNAL_INTERVAL", "20"))
INTEL_ANALYZE_INTERVAL = float(getenv("INTEL_ANALYZE_INTERVAL", "2"))

SELF_LABEL_IA = getenv("SELF_LABEL_IA", "Tiro seco por IA")

app = FastAPI(title="Fantan Guardião — FIRE-only (G0 + Recuperação oculta)", version="3.8.1")

# =========================
# DB bootstrap + migração
# =========================
def _ensure_db_dir():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def _migrate_old_db_if_needed():
    if os.path.exists(DB_PATH): return
    for src in ["/var/data/data.db","/opt/render/project/src/data.db","/opt/render/project/src/data/data.db","/data/data.db"]:
        if os.path.exists(src):
            _ensure_db_dir()
            try:
                shutil.copy2(src, DB_PATH)
                print(f"[DB] Migrado {src} -> {DB_PATH}")
                return
            except Exception as e:
                print(f"[DB] Erro migrando {src}: {e}")

_ensure_db_dir()
_migrate_old_db_if_needed()

def _connect():
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def _write(sql: str, params: tuple = (), tries=8, wait=0.25):
    for _ in range(tries):
        try:
            with _connect() as con:
                con.execute(sql, params)
            return
        except sqlite3.OperationalError as e:
            if any(k in str(e).lower() for k in ("locked","busy")):
                time.sleep(wait); continue
            raise
    raise sqlite3.OperationalError("Banco bloqueado após várias tentativas.")

def _all(sql: str, params: tuple = ()):
    with _connect() as con:
        return con.execute(sql, params).fetchall()

def _one(sql: str, params: tuple = ()):
    with _connect() as con:
        return con.execute(sql, params).fetchone()

def _scalar(sql: str, params: tuple = (), default=0):
    r = _one(sql, params)
    if not r: return default
    try:
        # retorna primeira coluna
        for v in r: return v if v is not None else default
    except Exception:
        pass
    # fallback por chave
    k = r.keys()
    return r[k[0]] if k and r[k[0]] is not None else default

def init_db():
    with _connect() as con:
        c = con.cursor()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS timeline (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, number INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ngram_stats (n INTEGER NOT NULL, ctx TEXT NOT NULL, next INTEGER NOT NULL, weight REAL NOT NULL, PRIMARY KEY (n, ctx, next));
        CREATE TABLE IF NOT EXISTS stats_pattern (pattern_key TEXT NOT NULL, number INTEGER NOT NULL, wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (pattern_key, number));
        CREATE TABLE IF NOT EXISTS stats_strategy (strategy TEXT NOT NULL, number INTEGER NOT NULL, wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (strategy, number));
        CREATE TABLE IF NOT EXISTS suggestions (source_msg_id INTEGER PRIMARY KEY, strategy TEXT, seq_raw TEXT, context_key TEXT, pattern_key TEXT, base TEXT, suggested_number INTEGER, stage TEXT, sent_at INTEGER);
        CREATE TABLE IF NOT EXISTS last_by_strategy (strategy TEXT PRIMARY KEY, source_msg_id INTEGER, suggested_number INTEGER, context_key TEXT, pattern_key TEXT, stage TEXT, created_at INTEGER);
        CREATE TABLE IF NOT EXISTS daily_score (yyyymmdd TEXT PRIMARY KEY, g0 INTEGER NOT NULL DEFAULT 0, g1 INTEGER NOT NULL DEFAULT 0, g2 INTEGER NOT NULL DEFAULT 0, loss INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS pending_outcome (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, strategy TEXT, suggested INTEGER NOT NULL, stage INTEGER NOT NULL, open INTEGER NOT NULL, window_left INTEGER NOT NULL, seen_numbers TEXT DEFAULT '', announced INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'CHAN');
        CREATE TABLE IF NOT EXISTS daily_score_ia (yyyymmdd TEXT PRIMARY KEY, g0 INTEGER NOT NULL DEFAULT 0, loss INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS recov_g1_stats (number INTEGER PRIMARY KEY, wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0);
        """)
        for ddl in (
            "ALTER TABLE pending_outcome ADD COLUMN seen_numbers TEXT DEFAULT ''",
            "ALTER TABLE pending_outcome ADD COLUMN announced INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE pending_outcome ADD COLUMN source TEXT NOT NULL DEFAULT 'CHAN'",
        ):
            try: c.execute(ddl)
            except sqlite3.OperationalError: pass

init_db()

# =========================
# Utils / Telegram
# =========================
now_ts = lambda: int(time.time())
utc_iso = lambda: datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
today_key = lambda: datetime.now(timezone.utc).strftime("%Y%m%d")

async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True},
        )

async def tg_broadcast(text: str, parse: str="HTML"):
    # Broadcast geral propositalmente desligado
    if REPL_ENABLED and REPL_CHANNEL:
        await tg_send_text(REPL_CHANNEL, text, parse)

# Envio exclusivo para IA FIRE/RESULT (com “ok”)
async def tg_send_fire(text: str, parse: str="HTML"):
    if IA_FIRE_CHANNEL:
        await tg_send_text(IA_FIRE_CHANNEL, text + "\n\nok", parse)

# Mensagens genéricas (mantidas, mas não vão ao canal secundário)
async def send_green_imediato(n:int, stage_txt:str="G0"): await tg_broadcast(f"✅ <b>GREEN</b> em <b>{stage_txt}</b> — Número: <b>{n}</b>")
async def send_loss_imediato(n:int, stage_txt:str="G0"):  await tg_broadcast(f"❌ <b>LOSS</b> — Número: <b>{n}</b> (em {stage_txt})")
async def send_recovery(_stage_txt:str): return

async def ia2_send_signal(best:int, conf:float, tail_len:int, mode:str):
    txt = (f"🤖 <b>{SELF_LABEL_IA} [{mode}]</b>\n"
           f"🎯 Número seco (G0): <b>{best}</b>\n"
           f"📈 Conf: <b>{conf*100:.2f}%</b> | Amostra≈<b>{tail_len}</b>")
    await tg_send_fire(txt)

async def ia2_send_result(win: bool, stage_txt: str, number: int):
    if win:
        await tg_send_fire(f"✅ <b>IA GREEN</b> em <b>{stage_txt}</b> — Número: <b>{number}</b>")
    else:
        await tg_send_fire(f"❌ <b>IA LOSS</b> em <b>{stage_txt}</b> — Número: <b>{number}</b>")

# =========================
# Timeline & n-grams
# =========================
def append_timeline(n: int):
    _write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_recent_tail(window: int = WINDOW) -> List[int]:
    return [r["number"] for r in _all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))][::-1]

def update_ngrams(decay: float = DECAY, max_n: int = 5, window: int = WINDOW):
    tail = get_recent_tail(window)
    if len(tail) < 2: return
    for t in range(1, len(tail)):
        nxt = tail[t]
        dist = (len(tail)-1) - t
        w = (decay ** dist)
        for n in range(2, max_n+1):
            i0 = t-(n-1)
            if i0 < 0: break
            ctx_key = ",".join(map(str, tail[i0:t]))
            _write("""INSERT INTO ngram_stats (n, ctx, next, weight) VALUES (?,?,?,?)
                      ON CONFLICT(n, ctx, next) DO UPDATE SET weight = weight + excluded.weight""",
                   (n, ctx_key, int(nxt), float(w)))

def prob_from_ngrams(ctx: List[int], candidate: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(map(str, ctx))
    tot = (_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key)) or {}).get("w") or 0.0
    if not tot: return 0.0
    w = (_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, candidate)) or {}).get("weight") or 0.0
    return (w / tot) if tot else 0.0

# =========================
# Parsers (canal)
# =========================
MUST_HAVE = (r"ENTRADA\s+CONFIRMADA", r"Mesa:\s*Fantan\s*-\s*Evolution")
MUST_NOT  = (r"\bANALISANDO\b", r"\bPlacar do dia\b", r"\bAPOSTA ENCERRADA\b")

def is_real_entry(t: str) -> bool:
    s = re.sub(r"\s+", " ", t).strip()
    if any(re.search(b, s, re.I) for b in MUST_NOT): return False
    if not all(re.search(g, s, re.I) for g in MUST_HAVE): return False
    return any(re.search(p, s, re.I) for p in [
        r"Sequ[eê]ncia:\s*[\d\s\|\-]+", r"\bKWOK\s*[1-4]\s*-\s*[1-4]", r"\bSS?H\s*[1-4](?:-[1-4]){0,3}",
        r"\bODD\b|\bEVEN\b", r"Entrar\s+ap[oó]s\s+o\s+[1-4]"
    ])

gx = [re.compile(r"APOSTA\s+ENCERRADA.*?\bGREEN\b.*?\((\d)\)", re.I|re.S),
      re.compile(r"\bGREEN\b.*?Número[:\s]*([1-4])", re.I|re.S),
      re.compile(r"\bGREEN\b.*?\(([1-4])\)", re.I|re.S)]
rx = [re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\((.*?)\)", re.I|re.S),
      re.compile(r"\bLOSS\b.*?Número[:\s]*([1-4])", re.I|re.S),
      re.compile(r"\bRED\b.*?\(([1-4])\)", re.I|re.S)]

def _first_14(s:str):
    n = re.findall(r"[1-4]", s)
    return int(n[0]) if n else None

def extract_green_number(t: str) -> Optional[int]:
    s = re.sub(r"\s+", " ", t)
    for r in gx:
        m = r.search(s)
        if m:
            v = _first_14(m.group(1))
            if v is not None: return v
    return None

def extract_red_last_left(t: str) -> Optional[int]:
    s = re.sub(r"\s+", " ", t)
    for r in rx:
        m = r.search(s)
        if m:
            v = _first_14(m.group(1))
            if v is not None: return v
    return None

def extract_strategy(t: str) -> Optional[str]:
    m = re.search(r"Estrat[eé]gia:\s*(\d+)", t, re.I); return m.group(1) if m else None

def extract_seq_raw(t: str) -> Optional[str]:
    m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", t, re.I); return m.group(1).strip() if m else None

def extract_after_num(t: str) -> Optional[int]:
    m = re.search(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", t, re.I); return int(m.group(1)) if m else None

def bases_from_sequence_left_recent(seq: List[int], k: int = 3) -> List[int]:
    out, seen = [], set()
    for n in seq:
        if n not in seen: seen.add(n); out.append(n)
        if len(out) == k: break
    return out

def parse_bases_and_pattern(t: str) -> Tuple[List[int], str]:
    s = re.sub(r"\s+", " ", t).strip()
    m = re.search(r"\bKWOK\s*([1-4])\s*-\s*([1-4])", s, re.I)
    if m: a,b = int(m.group(1)), int(m.group(2)); return [a,b], f"KWOK-{a}-{b}"
    if re.search(r"\bODD\b", s, re.I):  return [1,3], "ODD"
    if re.search(r"\bEVEN\b", s, re.I): return [2,4], "EVEN"
    m = re.search(r"\bSS?H\s*([1-4])(?:-([1-4]))?(?:-([1-4]))?(?:-([1-4]))?", s, re.I)
    if m:
      nums = [int(g) for g in m.groups() if g]; return nums, "SSH-" + "-".join(map(str, nums))
    m = re.search(r"Sequ[eê]ncia:\s*([\d\s\|\-]+)", s, re.I)
    if m:
        parts = list(map(int, re.findall(r"[1-4]", m.group(1))))
        base = bases_from_sequence_left_recent(parts, 3)
        if base: return base, "SEQ"
    return [], "GEN"

# =========================
# Estatísticas / placar
# =========================
def laplace_ratio(w:int, l:int) -> float: return (w + 1.0) / (w + l + 2.0)

def bump_pattern(pk:str, n:int, won:bool):
    row = _one("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pk, n))
    w,l = (row["wins"] if row else 0),(row["losses"] if row else 0)
    w,l = (w+1,l) if won else (w,l+1)
    _write("""INSERT INTO stats_pattern (pattern_key, number, wins, losses) VALUES (?,?,?,?)
              ON CONFLICT(pattern_key, number) DO UPDATE SET wins=excluded.wins, losses=excluded.losses""", (pk,n,w,l))

def bump_strategy(st:str, n:int, won:bool):
    row = _one("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?", (st, n))
    w,l = (row["wins"] if row else 0),(row["losses"] if row else 0)
    w,l = (w+1,l) if won else (w,l+1)
    _write("""INSERT INTO stats_strategy (strategy, number, wins, losses) VALUES (?,?,?,?)
              ON CONFLICT(strategy, number) DO UPDATE SET wins=excluded.wins, losses=excluded.losses""", (st,n,w,l))

def _upd_daily(table:str, stage: Optional[int], won: bool):
    y = today_key()
    row = _one(f"SELECT * FROM {table} WHERE yyyymmdd=?", (y,))
    if table == "daily_score":
        g0,g1,g2,loss,streak = (row["g0"],row["g1"],row["g2"],row["loss"],row["streak"]) if row else (0,0,0,0,0)
        if SCORE_G0_ONLY:
            if won and stage==0: g0,streak = g0+1, streak+1
            elif not won: loss,streak = loss+1, 0
        else:
            if won:
                if   stage==0: g0 += 1
                elif stage==1: g1 += 1
                elif stage==2: g2 += 1
                streak += 1
            else:
                loss += 1; streak = 0
        _write("""INSERT INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,?,?,?,?,?)
                  ON CONFLICT(yyyymmdd) DO UPDATE SET g0=excluded.g0,g1=excluded.g1,g2=excluded.g2,loss=excluded.loss,streak=excluded.streak""",
               (y,g0,g1,g2,loss,streak))
        total = (g0+loss) if SCORE_G0_ONLY else (g0+g1+g2+loss)
        acc = (g0/total) if total else 0.0
        return (g0,loss,acc,streak) if SCORE_G0_ONLY else (g0,g1,g2,loss,acc,streak)
    else:
        g0,loss,streak = (row["g0"],row["loss"],row["streak"]) if row else (0,0,0)
        if won and stage==0: g0,streak = g0+1, streak+1
        elif not won: loss,streak = loss+1, 0
        _write("""INSERT INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,?,?,?)
                  ON CONFLICT(yyyymmdd) DO UPDATE SET g0=excluded.g0,loss=excluded.loss,streak=excluded.streak""",
               (y,g0,loss,streak))
        total = g0+loss; acc = (g0/total) if total else 0.0
        return g0,loss,acc,streak

def update_daily_score(stage: Optional[int], won: bool):    return _upd_daily("daily_score", stage, won)
def update_daily_score_ia(stage: Optional[int], won: bool): return _upd_daily("daily_score_ia", stage, won)

def _convert_last_loss_to_green_table(table:str):
    y = today_key()
    row = _one(f"SELECT * FROM {table} WHERE yyyymmdd=?", (y,))
    if not row:
        if table=="daily_score":
            _write("INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,0,0,0,0,0)", (y,))
        else:
            _write("INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,0,0,0)", (y,))
        return
    if row["loss"] > 0:
        if table=="daily_score":
            _write("INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,?,?,?,?,?)",
                   (y, row["g0"]+1, row["g1"], row["g2"], row["loss"]-1, (row["streak"] or 0)+1))
        else:
            _write("INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,?,?,?)",
                   (y, row["g0"]+1, row["loss"]-1, (row["streak"] or 0)+1))

_convert_last_loss_to_green    = lambda: _convert_last_loss_to_green_table("daily_score")
_convert_last_loss_to_green_ia = lambda: _convert_last_loss_to_green_table("daily_score_ia")

async def _send_score_generic(table:str, prefix:str):
    # Mantido, mas silencioso (broadcast desativado)
    y = today_key()
    row = _one(f"SELECT * FROM {table} WHERE yyyymmdd=?", (y,))
    g0 = (row["g0"] if row else 0); loss = (row["loss"] if row else 0); streak = (row["streak"] if row else 0)
    acc = (g0/(g0+loss)*100) if (g0+loss) else 0.0
    await tg_broadcast(f"{prefix}\n🟢 G0:{g0}  🔴 Loss:{loss}\n✅ Acerto: {acc:.2f}%\n🔥 Streak: {streak} GREEN(s)")

async def send_scoreboard():    await _send_score_generic("daily_score", "📊 <b>Placar do dia</b>")
async def send_scoreboard_ia(): await _send_score_generic("daily_score_ia", "🤖 <b>Placar IA (dia)</b>")

def bump_recov_g1(n:int, won:bool):
    row = _one("SELECT wins, losses FROM recov_g1_stats WHERE number=?", (int(n),))
    w,l = (row["wins"] if row else 0, row["losses"] if row else 0)
    w,l = (w+1,l) if won else (w,l+1)
    _write("""INSERT INTO recov_g1_stats (number,wins,losses) VALUES (?,?,?)
              ON CONFLICT(number) DO UPDATE SET wins=excluded.wins, losses=excluded.losses""", (int(n),w,l))

# =========================
# Health + Reset diário (silenciosos)
# =========================
_ia2_last_reason, _ia2_last_reason_ts = "—", 0
def _mark_reason(r:str):
    global _ia2_last_reason, _ia2_last_reason_ts
    _ia2_last_reason, _ia2_last_reason_ts = r, now_ts()

def _fmt_bytes(n: int) -> str:
    n=float(n)
    for u in ["B","KB","MB","GB","TB","PB","EB"]:
        if n < 1024: return f"{n:.1f} {u}"
        n/=1024

def _dir_size_bytes(path: str) -> int:
    tot=0
    for root,_,files in os.walk(path):
        for f in files:
            try: tot += os.path.getsize(os.path.join(root,f))
            except: pass
    return tot

def _health_text() -> str:
    intel_size = _dir_size_bytes(INTEL_DIR)
    latest = os.path.join(INTEL_DIR, "snapshots", "latest_top.json")
    last_snap = datetime.utcfromtimestamp(os.path.getmtime(latest)).replace(tzinfo=timezone.utc).isoformat() if os.path.exists(latest) else "—"
    g0= _scalar("SELECT g0 FROM daily_score WHERE yyyymmdd=?", (today_key(),), 0)
    loss= _scalar("SELECT loss FROM daily_score WHERE yyyymmdd=?", (today_key(),), 0)
    total = g0+loss; acc = (g0/total*100) if total else 0.0
    try: age = max(0, now_ts()-(_ia2_last_reason_ts or now_ts()))
    except: age = 0
    return (
        "🩺 <b>Saúde do Guardião</b>\n"
        f"⏱️ UTC: <code>{utc_iso()}</code>\n—\n"
        f"🗄️ timeline: <b>{_scalar('SELECT COUNT(*) FROM timeline')}</b>\n"
        f"📚 ngram_stats: <b>{_scalar('SELECT COUNT(*) FROM ngram_stats')}</b> | amostras≈<b>{int(_scalar('SELECT SUM(weight) FROM ngram_stats'))}</b>\n"
        f"⏳ pendências abertas: <b>{_scalar('SELECT COUNT(*) FROM pending_outcome WHERE open=1')}</b>\n"
        f"💾 INTEL dir: <b>{_fmt_bytes(intel_size)}</b> | último snapshot: <code>{last_snap}</code>\n—\n"
        f"📊 Placar (hoje - G0 only): G0=<b>{g0}</b> | Loss=<b>{loss}</b>\n"
        f"🤖 Motivo último NÃO-FIRE/FIRE: {_ia2_last_reason} (há {age}s)\n"
    )

async def _health_reporter_task():
    while True:
        try:
            await tg_broadcast(_health_text())  # silencioso (REPL_ENABLED False)
        except Exception as e:
            print(f"[HEALTH] erro: {e}")
        await asyncio.sleep(1800)

async def _daily_reset_task():
    while True:
        try:
            now = datetime.now(timezone.utc)
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            await asyncio.sleep(max(1.0, (tomorrow - now).total_seconds()))
            y = today_key()
            _write("INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,0,0,0,0,0)", (y,))
            _write("INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,0,0,0)", (y,))
            await tg_broadcast("🕛 Reset diário executado (00:00 UTC)")  # silencioso
        except Exception as e:
            print(f"[RESET] erro: {e}")
            await asyncio.sleep(60)

# =========================
# Pendências (G0 + recuperação oculta) + RESULTADOS IA
# =========================
def open_pending(strategy: Optional[str], suggested: int, source: str = "CHAN"):
    _write("""INSERT INTO pending_outcome
              (created_at,strategy,suggested,stage,open,window_left,seen_numbers,announced,source)
              VALUES (?,?,?,?,1,?, '', 0, ?)""",
           (now_ts(), strategy or "", int(suggested), 0, MAX_STAGE, source))
    try: INTEL.start_signal(suggested=int(suggested), strategy=strategy)
    except Exception as e: print(f"[INTEL] start_signal error: {e}")

def _has_open_pending() -> bool: return bool(_scalar("SELECT COUNT(*) FROM pending_outcome WHERE open=1"))

def _recov_mark(stage:int, sug:int, won:bool):
    if stage == 1:
        try: bump_recov_g1(sug, won)
        except Exception as e: print(f"[RECOV_G1] erro: {e}")

async def close_pending_with_result(n_real: int, event_kind: str):
    rows = _all("""SELECT id,strategy,suggested,stage,open,window_left,seen_numbers,announced,source
                   FROM pending_outcome WHERE open=1 ORDER BY id""")
    for r in rows:
        pid, strat, sug, stage = int(r["id"]), (r["strategy"] or ""), int(r["suggested"]), int(r["stage"])
        left, announced, source = int(r["window_left"]), int(r["announced"]), (r["source"] or "CHAN").upper()
        seen_txt = (r["seen_numbers"] or "")
        seen_txt2 = (seen_txt + ("|" if seen_txt else "") + str(n_real))
        _write("UPDATE pending_outcome SET seen_numbers=? WHERE id=?", (seen_txt2, pid))

        hit = (n_real == sug)

        if announced == 0:
            # Primeira janela (G0) — público do canal original foi desativado; IA envia no canal secundário
            bump_pattern("PEND", sug, hit)
            if strat: bump_strategy(strat, sug, hit)
            update_daily_score(0 if hit else None, hit)
            if source == "IA":
                update_daily_score_ia(0 if hit else None, hit)
                # Resultado IA em G0
                await ia2_send_result(hit, "G0", sug)

            if hit:
                _write("UPDATE pending_outcome SET announced=1, open=0 WHERE id=?", (pid,))
            else:
                _write("UPDATE pending_outcome SET announced=1 WHERE id=?", (pid,))
                left -= 1
                if left <= 0 or MAX_STAGE <= 1:
                    _write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
                else:
                    _write("UPDATE pending_outcome SET window_left=?, stage=? WHERE id=?", (left, min(stage+1, MAX_STAGE-1), pid))
        else:
            # Recuperação (G1/G2) — anteriormente silenciosa; agora reportamos IA no canal secundário
            left -= 1
            if hit:
                try:
                    _convert_last_loss_to_green()
                    if source == "IA":
                        _convert_last_loss_to_green_ia()
                except Exception as e:
                    print(f"[SCORE] convert loss->green erro: {e}")
                _recov_mark(stage, sug, True)
                bump_pattern("RECOV", sug, True)
                if strat: bump_strategy(strat, sug, True)
                _write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
                if source == "IA":
                    await ia2_send_result(True, f"G{stage}", sug)
            else:
                if left <= 0:
                    _recov_mark(stage, sug, False)
                    _write("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
                    if source == "IA":
                        await ia2_send_result(False, f"G{stage}", sug)
                else:
                    _write("UPDATE pending_outcome SET window_left=?, stage=? WHERE id=?", (left, min(stage+1, MAX_STAGE-1), pid))

    try:
        if not _one("SELECT 1 FROM pending_outcome WHERE open=1"):
            INTEL.stop_signal()
    except Exception as e:
        print(f"[INTEL] stop_signal check error: {e}")

# =========================
# Predição (número seco)
# =========================
def ngram_backoff_score(tail: List[int], after_num: Optional[int], c: int) -> float:
    if not tail: return 0.0
    if after_num is not None:
        idxs = [i for i,v in enumerate(tail) if v == after_num]
        if not idxs:
            ctx4=tail[-4:] if len(tail)>=4 else []; ctx3=tail[-3:] if len(tail)>=3 else []; ctx2=tail[-2:] if len(tail)>=2 else []; ctx1=tail[-1:] if len(tail)>=1 else []
        else:
            i = idxs[-1]
            ctx1 = tail[max(0,i):i+1]
            ctx2 = tail[max(0,i-1):i+1] if i-1>=0 else []
            ctx3 = tail[max(0,i-2):i+1] if i-2>=0 else []
            ctx4 = tail[max(0,i-3):i+1] if i-3>=0 else []
    else:
        ctx4=tail[-4:] if len(tail)>=4 else []; ctx3=tail[-3:] if len(tail)>=3 else []; ctx2=tail[-2:] if len(tail)>=2 else []; ctx1=tail[-1:] if len(tail)>=1 else []
    parts = []
    if len(ctx4)==4: parts.append((W4, prob_from_ngrams(ctx4[:-1], c)))
    if len(ctx3)==3: parts.append((W3, prob_from_ngrams(ctx3[:-1], c)))
    if len(ctx2)==2: parts.append((W2, prob_from_ngrams(ctx2[:-1], c)))
    if len(ctx1)==1: parts.append((W1, prob_from_ngrams(ctx1[:-1], c)))
    return sum(w*p for w,p in parts)

def confident_best(post: Dict[int,float], gap: float = GAP_MIN) -> Optional[int]:
    a = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a: return None
    if len(a)==1: return a[0][0]
    return a[0][0] if (a[0][1]-a[1][1]) >= gap else None

def suggest_number(base: List[int], pattern_key: str, strategy: Optional[str], after_num: Optional[int]) -> Tuple[Optional[int],float,int,Dict[int,float]]:
    if not base: base = [1,2,3,4]
    tail = get_recent_tail(WINDOW)

    # regras "após X"
    if after_num is not None:
        try: last_idx = max(i for i,v in enumerate(tail) if v==after_num)
        except ValueError: return None,0.0,len(tail),{k:1/len(base) for k in base}
        if (len(tail)-1-last_idx) > 60: return None,0.0,len(tail),{k:1/len(base) for k in base}

    scores: Dict[int,float] = {}
    hour_block = int(datetime.now(timezone.utc).hour // 2)
    pat_key = f"{pattern_key}|h{hour_block}"

    for c in base:
        ng = ngram_backoff_score(tail, after_num, c)

        rowp = _one("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pat_key, c))
        pw, pl = (rowp["wins"] if rowp else 0), (rowp["losses"] if rowp else 0)
        p_pat = laplace_ratio(pw, pl)

        if strategy:
            rows = _one("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?", (strategy, c))
            sw, sl = (rows["wins"] if rows else 0), (rows["losses"] if rows else 0)
            p_str = laplace_ratio(sw, sl)
        else:
            p_str = 1.0/len(base)

        boost = 1.05 if p_pat >= 0.60 else (0.97 if p_pat <= 0.40 else 1.0)
        prior = 1.0/len(base)
        scores[c] = prior * ((ng or 1e-6) ** ALPHA) * (p_pat ** BETA) * (p_str ** GAMMA) * boost

    total = sum(scores.values()) or 1e-9
    post = {k: v/total for k,v in scores.items()}

    number = confident_best(post, gap=GAP_MIN)
    conf = post.get(number, 0.0) if number is not None else 0.0

    samples = int(((_one("SELECT SUM(weight) AS s FROM ngram_stats") or {}).get("s") or 0))

    # Anti-repique: se último anunciado fechou LOSS com mesmo número, exija mais confiança
    last = _one("SELECT suggested, announced FROM pending_outcome ORDER BY id DESC LIMIT 1")
    if last and number is not None and last["suggested"] == number and (last["announced"] or 0) == 1:
        if post.get(number, 0.0) < (MIN_CONF_G0 + 0.08):
            return None, 0.0, samples, post

    # Filtro de qualidade (modo equilibrado)
    top2 = sorted(post.items(), key=lambda kv: kv[1], reverse=True)[:2]
    gap = (top2[0][1] - (top2[1][1] if len(top2) > 1 else 0.0)) if top2 else 0.0
    enough_samples = samples >= MIN_SAMPLES
    should_abstain = enough_samples and ((number is None) or (post.get(number, 0.0) < MIN_CONF_G0) or (gap < MIN_GAP_G0))
    if should_abstain:
        return None, 0.0, samples, post

    return number, conf, samples, post

def build_suggestion_msg(number:int, base:List[int], pattern_key:str, after_num:Optional[int], conf:float, samples:int, stage:str="G0")->str:
    base_txt = ", ".join(map(str, base)) if base else "—"
    aft_txt = f" após {after_num}" if after_num else ""
    return f"🎯 <b>Número seco ({stage}):</b> <b>{number}</b>\n🧩 <b>Padrão:</b> {pattern_key}{aft_txt}\n🧮 <b>Base:</b> [{base_txt}]\n📊 Conf: {conf*100:.2f}% | Amostra≈{samples}"

# =========================
# IA2 — FIRE-only
# =========================
IA2_TIER_STRICT, IA2_GAP_SAFETY, IA2_DELTA_GAP = 0.62, 0.08, 0.03
IA2_MAX_PER_HOUR, IA2_COOLDOWN_AFTER_LOSS, IA2_MIN_SECONDS_BETWEEN_FIRE = 10, 20, 15
_ia2_blocked_until_ts, _ia2_sent_this_hour, _ia2_hour_bucket, _ia2_last_fire_ts = 0,0,None,0

def _ia2_hour_key() -> int: return int(datetime.now(timezone.utc).strftime("%Y%m%d%H"))
def _ia2_reset_hour():
    global _ia2_sent_this_hour, _ia2_hour_bucket
    hb = _ia2_hour_key()
    if _ia2_hour_bucket != hb: _ia2_hour_bucket, _ia2_sent_this_hour = hb, 0
def _ia2_antispam_ok() -> bool: _ia2_reset_hour(); return _ia2_sent_this_hour < IA2_MAX_PER_HOUR
def _ia2_mark_sent(): 
    global _ia2_sent_this_hour; _ia2_sent_this_hour += 1
def _ia2_blocked_now() -> bool: return now_ts() < _ia2_blocked_until_ts
def _ia_set_post_loss_block():
    global _ia2_blocked_until_ts; _ia2_blocked_until_ts = now_ts() + int(IA2_COOLDOWN_AFTER_LOSS)
def _ia2_can_fire_now() -> bool:
    return (not _ia2_blocked_now()) and _ia2_antispam_ok() and (now_ts() - _ia2_last_fire_ts >= IA2_MIN_SECONDS_BETWEEN_FIRE)
def _ia2_mark_fire_sent():
    global _ia2_last_fire_ts; _ia2_mark_sent(); _ia2_last_fire_ts = now_ts()

def _relevance_ok(conf_raw: float, gap: float, tail_len: int) -> bool:
    return ((conf_raw >= IA2_TIER_STRICT) or (conf_raw >= IA2_TIER_STRICT-IA2_DELTA_GAP and gap >= IA2_GAP_SAFETY)) and (tail_len >= MIN_SAMPLES)

async def ia2_process_once():
    # Base GEN (sem restrição)
    base, pattern_key, strategy, after_num = [], "GEN", None, None
    tail = get_recent_tail(WINDOW)
    best, conf_raw, tail_len, post = suggest_number(base, pattern_key, strategy, after_num)

    if best is None:
        _mark_reason(f"amostra_insuficiente({tail_len}<{MIN_SAMPLES})" if tail_len < MIN_SAMPLES else "sem_numero_confiavel(conf/gap)")
        return

    gap = 1.0
    if post and len(post) >= 2:
        top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
        if len(top) >= 2: gap = top[0][1] - top[1][1]

    if _has_open_pending():
        _mark_reason("pendencia_aberta(aguardando G1/G2)"); return
    if _ia2_blocked_now():
        _mark_reason("cooldown_pos_loss"); return
    if not _ia2_antispam_ok():
        _mark_reason("limite_hora_atingido"); return
    if now_ts() - _ia2_last_fire_ts < IA2_MIN_SECONDS_BETWEEN_FIRE:
        _mark_reason("espacamento_minimo"); return

    if _relevance_ok(conf_raw, gap, tail_len) and _ia2_can_fire_now():
        open_pending(strategy, best, source="IA")
        await ia2_send_signal(best, conf_raw, tail_len, "FIRE")
        _ia2_mark_fire_sent()
        _mark_reason(f"FIRE(best={best}, conf={conf_raw:.3f}, gap={gap:.3f}, tail={tail_len})")
        return

    _mark_reason(f"reprovado_relevancia(conf={conf_raw:.3f}, gap={gap:.3f}, tail={tail_len})")

# =========================
# INTEL (stub)
# =========================
class _IntelStub:
    def __init__(self, base_dir:str):
        self.base_dir = base_dir
        os.makedirs(os.path.join(self.base_dir, "snapshots"), exist_ok=True)
        self._signal_active = False
    def start_signal(self, suggested:int, strategy:Optional[str]=None):
        self._signal_active = True
        try:
            with open(os.path.join(self.base_dir, "snapshots", "latest_top.json"), "w") as f:
                json.dump({"ts": now_ts(), "suggested": suggested, "strategy": strategy}, f)
        except Exception as e: print(f"[INTEL] snapshot error: {e}")
    def stop_signal(self): self._signal_active = False

INTEL = _IntelStub(INTEL_DIR)

# =========================
# Relatório (mantido, silencioso)
# =========================
def _sum_days(table:str, days:int):
    rows = _all(f"SELECT yyyymmdd, g0, loss FROM {table} ORDER BY yyyymmdd DESC LIMIT ?", (days,))
    g0 = sum((r["g0"] or 0) for r in rows)
    loss = sum((r["loss"] or 0) for r in rows)
    acc = (g0/(g0+loss)*100.0) if (g0+loss) else 0.0
    return g0, loss, acc

def _mk_relatorio_text(days:int=7)->str:
    days = max(1, min(int(days), 30))
    y = today_key()
    r  = _one("SELECT g0,loss FROM daily_score   WHERE yyyymmdd=?", (y,)) or {"g0":0,"loss":0}
    ri = _one("SELECT g0,loss FROM daily_score_ia WHERE yyyymmdd=?", (y,)) or {"g0":0,"loss":0}
    g0,loss = r["g0"], r["loss"]; ia_g0, ia_loss = ri["g0"], ri["loss"]
    acc_today = (g0/(g0+loss)*100.0) if (g0+loss) else 0.0
    ia_acc_today = (ia_g0/(ia_g0+ia_loss)*100.0) if (ia_g0+ia_loss) else 0.0
    sum_g0,sum_loss,acc_nd = _sum_days("daily_score", days)
    sum_ia_g0,sum_ia_loss,ia_acc_nd = _sum_days("daily_score_ia", days)
    return ("📈 <b>Relatório de Desempenho</b>\n"
            f"🗓️ Janela: <b>hoje</b> e últimos <b>{days}</b> dias\n—\n"
            f"📣 <b>Canal (hoje)</b>: G0=<b>{g0}</b> | Loss=<b>{loss}</b> | Acerto=<b>{acc_today:.2f}%</b>\n"
            f"🤖 <b>IA (hoje)</b>: G0=<b>{ia_g0}</b> | Loss=<b>{ia_loss}</b> | Acerto=<b>{ia_acc_today:.2f}%</b>\n—\n"
            f"📣 <b>Canal ({days}d)</b>: G0=<b>{sum_g0}</b> | Loss=<b>{sum_loss}</b> | Acerto=<b>{acc_nd:.2f}%</b>\n"
            f"🤖 <b>IA ({days}d)</b>: G0=<b>{sum_ia_g0}</b> | Loss=<b>{sum_ia_loss}</b> | Acerto=<b>{ia_acc_nd:.2f}%</b>\n")

# =========================
# Webhook / API
# =========================
class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

@app.get("/")
async def root(): return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>"}

@app.on_event("startup")
async def _boot_tasks():
    try: asyncio.create_task(_health_reporter_task())
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

    t = re.sub(r"\s+", " ", (msg.get("text") or msg.get("caption") or "").strip())

    # GREEN / RED — registra número real e fecha/atualiza pendências
    gnum, redn = extract_green_number(t), extract_red_last_left(t)
    if gnum is not None or redn is not None:
        n_observed = gnum if gnum is not None else redn
        event_kind = "GREEN" if gnum is not None else "RED"
        append_timeline(n_observed)
        update_ngrams()
        await close_pending_with_result(n_observed, event_kind)

        strat = extract_strategy(t) or ""
        row = _one("SELECT suggested_number, pattern_key FROM last_by_strategy WHERE strategy=?", (strat,))
        if row and gnum is not None:
            suggested = int(row["suggested_number"])
            pat_key   = row["pattern_key"] or "GEN"
            won = (suggested == int(gnum))
            bump_pattern(pat_key, suggested, won)
            if strat: bump_strategy(strat, suggested, won)

        await ia2_process_once()
        return {"ok": True, "observed": n_observed, "kind": event_kind}

    # ANALISANDO — alimenta timeline
    if re.search(r"\bANALISANDO\b", t, re.I):
        seq_raw = extract_seq_raw(t)
        if seq_raw:
            for n in list(map(int, re.findall(r"[1-4]", seq_raw)))[::-1]:
                append_timeline(n)
            update_ngrams()
        await ia2_process_once()
        return {"ok": True, "analise": True}

    # ENTRADA CONFIRMADA — processa G0 do canal (publicação do canal está desativada)
    if not is_real_entry(t):
        await ia2_process_once()
        return {"ok": True, "skipped": True}

    source_msg_id = msg.get("message_id")
    if _one("SELECT 1 FROM suggestions WHERE source_msg_id=?", (source_msg_id,)):
        await ia2_process_once()
        return {"ok": True, "dup": True}

    strategy  = extract_strategy(t) or ""
    seq_raw   = extract_seq_raw(t) or ""
    after_num = extract_after_num(t)
    base, pattern_key = parse_bases_and_pattern(t)
    if not base: base=[1,2,3,4]; pattern_key="GEN"

    number, conf, samples, post = suggest_number(base, pattern_key, strategy, after_num)
    if number is None:
        await ia2_process_once()
        return {"ok": True, "skipped_low_conf": True}

    _write("""INSERT OR REPLACE INTO suggestions
              (source_msg_id, strategy, seq_raw, context_key, pattern_key, base, suggested_number, stage, sent_at)
              VALUES (?,?,?,?,?,?,?,?,?)""",
           (source_msg_id, strategy, seq_raw, "CTX", pattern_key, json.dumps(base), int(number), "G0", now_ts()))
    _write("""INSERT OR REPLACE INTO last_by_strategy
              (strategy, source_msg_id, suggested_number, context_key, pattern_key, stage, created_at)
              VALUES (?,?,?,?,?,?,?)""",
           (strategy, source_msg_id, int(number), "CTX", pattern_key, "G0", now_ts()))

    open_pending(strategy, int(number), source="CHAN")

    # Publicação do canal original desativada propositalmente; IA vai publicar apenas no IA_FIRE_CHANNEL
    await ia2_process_once()
    return {"ok": True, "sent": True, "number": number, "conf": conf, "samples": samples}

# =========================
# DEBUG
# =========================
@app.get("/debug/samples")
async def debug_samples():
    samples = int((_one("SELECT SUM(weight) AS s FROM ngram_stats") or {}).get("s") or 0)
    return {"samples": samples, "MIN_SAMPLES": MIN_SAMPLES, "enough_samples": samples >= MIN_SAMPLES}

@app.get("/debug/reason")
async def debug_reason():
    try: return {"last_reason": _ia2_last_reason, "last_reason_age_seconds": max(0, now_ts()-(_ia2_last_reason_ts or now_ts()))}
    except Exception as e: return {"error": str(e)}

@app.get("/debug/state")
async def debug_state():
    try:
        samples = int((_one("SELECT SUM(weight) AS s FROM ngram_stats") or {}).get("s") or 0)
        pend_open = _scalar("SELECT COUNT(*) FROM pending_outcome WHERE open=1")
        reason_age = max(0, now_ts()-(_ia2_last_reason_ts or now_ts()))
        y = today_key()
        r = _one("SELECT g0,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,)) or {"g0":0,"loss":0,"streak":0}
        ria = _one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,)) or {"g0":0,"loss":0,"streak":0}
        acc = (r["g0"]/max(1,(r["g0"]+r["loss"])))
        ia_acc = (ria["g0"]/max(1,(ria["g0"]+ria["loss"])))
        cooldown_remaining = max(0, (_ia2_blocked_until_ts or 0) - now_ts())
        last_fire_age = max(0, now_ts() - (_ia2_last_fire_ts or 0))
        cfg = {
            "MIN_SAMPLES": MIN_SAMPLES,
            "IA2_TIER_STRICT": IA2_TIER_STRICT, "IA2_GAP_SAFETY": IA2_GAP_SAFETY, "IA2_DELTA_GAP": IA2_DELTA_GAP,
            "IA2_MAX_PER_HOUR": IA2_MAX_PER_HOUR, "IA2_MIN_SECONDS_BETWEEN_FIRE": IA2_MIN_SECONDS_BETWEEN_FIRE,
            "IA2_COOLDOWN_AFTER_LOSS": IA2_COOLDOWN_AFTER_LOSS, "INTEL_ANALYZE_INTERVAL": INTEL_ANALYZE_INTERVAL,
        }
        return {
            "samples": samples, "enough_samples": samples >= MIN_SAMPLES, "pendencias_abertas": int(pend_open),
            "last_reason": _ia2_last_reason, "last_reason_age_seconds": int(reason_age),
            "hour_bucket": _ia2_hour_key(), "fires_enviados_nesta_hora": _ia2_sent_this_hour,
            "cooldown_pos_loss_seconds": int(cooldown_remaining), "ultimo_fire_ha_seconds": int(last_fire_age),
            "placar_canal_hoje": {"g0": int(r["g0"]), "loss": int(r["loss"]), "acc_pct": round(acc*100, 2)},
            "placar_ia_hoje": {"g0": int(ria["g0"]), "loss": int(ria["loss"]), "acc_pct": round(ia_acc*100, 2)},
            "config": cfg,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

_last_flush_ts = 0
@app.get("/debug/flush")
async def debug_flush(request: Request, days: int = 7, key: str = ""):
    global _last_flush_ts
    if not key or key != FLUSH_KEY: return {"ok": False, "error": "unauthorized"}
    now = now_ts()
    if now - (_last_flush_ts or 0) < 60:
        return {"ok": False, "error": "flush_cooldown", "retry_after_seconds": 60 - (now - (_last_flush_ts or 0))}
    try: await tg_broadcast(_health_text())
    except Exception as e: print(f"[FLUSH] saúde: {e}")
    try: await tg_broadcast(_mk_relatorio_text(max(1, min(days, 30))))
    except Exception as e: print(f"[FLUSH] relatório: {e}")
    _last_flush_ts = now
    return {"ok": True, "flushed": True, "days": max(1, min(days, 30))}
