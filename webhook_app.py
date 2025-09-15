# -*- coding: utf-8 -*-
"""
Fantan Guardião — Webhook (G0/G1/G2 VISÍVEL) + IA FIRE-only
-----------------------------------------------------------
Rotas:
  GET  /                      -> ping
  GET  /health                -> snapshot de saúde
  GET  /debug/state           -> estado (samples, fase, placar)
  GET  /debug/flush?key=...   -> envia saúde + relatório no canal réplica (antispam)
  POST /webhook/{WEBHOOK_TOKEN} -> recebe updates do Telegram

ENV obrigatórias:
  TG_BOT_TOKEN, WEBHOOK_TOKEN, PUBLIC_CHANNEL
ENV opcionais:
  DB_PATH (default: /var/data/data.db)
  REPL_CHANNEL (canal espelho para broadcasts; se vazio, usa PUBLIC_CHANNEL)
  FLUSH_KEY (default: meusegredo123)
  INTEL_DIR (default: /var/data/ai_intel)
"""

import os, re, json, time, sqlite3, asyncio, shutil
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone, timedelta
from collections import Counter

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# =========================
# ENV / CONFIG
# =========================
DB_PATH        = os.getenv("DB_PATH", "/var/data/data.db").strip() or "/var/data/data.db"
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()   # -100... ou @canal
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
REPL_CHANNEL   = os.getenv("REPL_CHANNEL", "").strip() or PUBLIC_CHANNEL
FLUSH_KEY      = os.getenv("FLUSH_KEY", "meusegredo123").strip()

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN or not PUBLIC_CHANNEL:
    raise RuntimeError("Defina TG_BOT_TOKEN, WEBHOOK_TOKEN e PUBLIC_CHANNEL nas variáveis de ambiente.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# =========================
# MODO / Hiperparâmetros
# =========================
MAX_STAGE       = 3   # 3 estágios => G0, G1, G2
FAST_RESULT     = True
SCORE_G0_ONLY   = False  # AGORA: placar do canal conta G0/G1/G2/LOSS (tudo visível)

# Predição (n-grams)
WINDOW = 400
DECAY  = 0.985
W4, W3, W2, W1 = 0.38, 0.30, 0.20, 0.12
ALPHA, BETA, GAMMA = 1.05, 0.70, 0.40
GAP_MIN = 0.08
MIN_SAMPLES = 1000

# IA (antispam/fases)
IA2_GAP_SAFETY = 0.08
IA2_TIER_STRICT = 0.62
IA2_DELTA_GAP   = 0.03
IA2_MAX_PER_HOUR = 12
IA2_COOLDOWN_AFTER_LOSS = 15
IA2_MIN_SECONDS_BETWEEN_FIRE = 12

# INTEL
INTEL_DIR = os.getenv("INTEL_DIR", "/var/data/ai_intel").rstrip("/")
os.makedirs(os.path.join(INTEL_DIR, "snapshots"), exist_ok=True)

# =========================
# App
# =========================
app = FastAPI(title="Fantan Guardião — G0/G1/G2 VISÍVEL", version="4.0.0")

# =========================
# SQLite (WAL/timeout)
# =========================
def _ensure_db_dir():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def _connect() -> sqlite3.Connection:
    _ensure_db_dir()
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=10000;")
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
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_outcome (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
        strategy TEXT, suggested INTEGER NOT NULL, stage INTEGER NOT NULL,
        open INTEGER NOT NULL, window_left INTEGER NOT NULL,
        seen_numbers TEXT DEFAULT '', announced INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'CHAN')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY, g0 INTEGER NOT NULL DEFAULT 0,
        g1 INTEGER NOT NULL DEFAULT 0, g2 INTEGER NOT NULL DEFAULT 0,
        loss INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score_ia (
        yyyymmdd TEXT PRIMARY KEY, g0 INTEGER NOT NULL DEFAULT 0,
        loss INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0)""")
    con.commit(); con.close()

init_db()

# =========================
# Utils / Tempo / Placar
# =========================
def now_ts() -> int: return int(time.time())
def utc_iso() -> str: return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
def today_key() -> str: return datetime.now(timezone.utc).strftime("%Y%m%d")

def update_daily_score(stage: Optional[int], won: bool):
    y = today_key()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0,g1,g2,loss,streak = (row["g0"],row["g1"],row["g2"],row["loss"],row["streak"]) if row else (0,0,0,0,0)
    if won:
        if stage == 0: g0 += 1
        elif stage == 1: g1 += 1
        elif stage == 2: g2 += 1
        streak += 1
    else:
        loss += 1
        streak = 0
    exec_write("""INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
                  VALUES (?,?,?,?,?,?)""", (y,g0,g1,g2,loss,streak))

def update_daily_score_ia(stage: Optional[int], won: bool):
    # IA mantém G0/Loss
    y = today_key()
    row = query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    g0,loss,streak = (row["g0"],row["loss"],row["streak"]) if row else (0,0,0)
    if won and stage == 0:
        g0 += 1; streak += 1
    elif not won and stage == 0:
        loss += 1; streak = 0
    exec_write("""INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak)
                  VALUES (?,?,?,?)""", (y,g0,loss,streak))

def _daily_score_snapshot():
    y = today_key()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0 = row["g0"] if row else 0
    g1 = row["g1"] if row else 0
    g2 = row["g2"] if row else 0
    loss = row["loss"] if row else 0
    streak = row["streak"] if row else 0
    greens_total = g0 + g1 + g2
    total = greens_total + loss
    acc = (greens_total/total*100.0) if total else 0.0
    return g0, g1, g2, loss, streak, greens_total, total, acc

def _score_line() -> str:
    g0,g1,g2,loss,_,greens,total,acc = _daily_score_snapshot()
    return f"{greens} GREEN × {loss} LOSS — {acc:.1f}% (G0:{g0} G1:{g1} G2:{g2})"

# =========================
# Telegram helpers
# =========================
async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse,
                                "disable_web_page_preview": True})

async def tg_broadcast(text: str, parse: str="HTML"):
    await tg_send_text(REPL_CHANNEL or PUBLIC_CHANNEL, text, parse)

# =========================
# Timeline & n-grams
# =========================
def append_timeline(n: int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_recent_tail(window: int = WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def prob_from_ngrams(ctx: List[int], candidate: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    row_tot = query_one("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key))
    tot = (row_tot["w"] or 0.0) if row_tot else 0.0
    if tot <= 0: return 0.0
    row = query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, int(candidate)))
    w = (row["weight"] or 0.0) if row else 0.0
    return w / tot

def update_ngrams(decay: float = DECAY, max_n: int = 5, window: int = WINDOW):
    tail = get_recent_tail(window)
    if len(tail) < 2: return
    for t in range(1, len(tail)):
        nxt = int(tail[t]); dist = (len(tail)-1) - t; w = (decay ** dist)
        for n in range(2, max_n+1):
            if t-(n-1) < 0: break
            ctx = tail[t-(n-1):t]; ctx_key = ",".join(str(x) for x in ctx)
            exec_write("""INSERT INTO ngram_stats (n, ctx, next, weight)
                          VALUES (?,?,?,?)
                          ON CONFLICT(n, ctx, next) DO UPDATE SET weight=weight+excluded.weight
                       """, (n, ctx_key, nxt, float(w)))

# Boost leve dos 2 mais frequentes da cauda curta
def tail_top2_boost(tail: List[int], k:int=40) -> Dict[int, float]:
    boosts = {1:1.00,2:1.00,3:1.00,4:1.00}
    t = tail[-k:] if len(tail)>=k else tail[:]
    c = Counter(t)
    if not c: return boosts
    most = c.most_common()
    if len(most)>=1: boosts[most[0][0]] = 1.04
    if len(most)>=2: boosts[most[1][0]] = 1.02
    return boosts

def ngram_backoff_score(tail: List[int], after_num: Optional[int], candidate: int) -> float:
    if not tail: return 0.0
    # contextos
    if after_num is not None and after_num in tail:
        i = max([idx for idx,v in enumerate(tail) if v==after_num])
        ctx1 = tail[max(0,i):i+1]
        ctx2 = tail[max(0,i-1):i+1] if i-1>=0 else []
        ctx3 = tail[max(0,i-2):i+1] if i-2>=0 else []
        ctx4 = tail[max(0,i-3):i+1] if i-3>=0 else []
    else:
        ctx4 = tail[-4:] if len(tail)>=4 else []
        ctx3 = tail[-3:] if len(tail)>=3 else []
        ctx2 = tail[-2:] if len(tail)>=2 else []
        ctx1 = tail[-1:] if len(tail)>=1 else []
    score = 0.0
    parts = []
    if len(ctx4)==4: parts.append((W4, prob_from_ngrams(ctx4[:-1], candidate)))
    if len(ctx3)==3: parts.append((W3, prob_from_ngrams(ctx3[:-1], candidate)))
    if len(ctx2)==2: parts.append((W2, prob_from_ngrams(ctx2[:-1], candidate)))
    if len(ctx1)==1: parts.append((W1, prob_from_ngrams(ctx1[:-1], candidate)))
    for w,p in parts: score += w*(p or 0.0)
    return score

def suggest_number(base: List[int], after_num: Optional[int]) -> Tuple[Optional[int],float,int,Dict[int,float]]:
    base = base or [1,2,3,4]
    tail = get_recent_tail(WINDOW)
    boosts = tail_top2_boost(tail, k=40)
    scores: Dict[int,float] = {}

    if after_num is not None:
        try:
            last = max([i for i,v in enumerate(tail) if v==after_num])
            if (len(tail)-1-last) > 60:
                return None, 0.0, len(tail), {b:1/len(base) for b in base}
        except ValueError:
            return None, 0.0, len(tail), {b:1/len(base) for b in base}

    for c in base:
        ng = ngram_backoff_score(tail, after_num, c)
        prior = 1.0/len(base)
        scores[c] = prior * ((ng or 1e-6) ** ALPHA) * boosts.get(c,1.0)

    total = sum(scores.values()) or 1e-9
    post = {k: v/total for k,v in scores.items()}
    top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not top: return None, 0.0, len(tail), post
    best = top[0][0]
    conf = post[best]
    gap  = top[0][1] - (top[1][1] if len(top)>1 else 0.0)

    # filtros
    samples = int((query_one("SELECT SUM(weight) AS s FROM ngram_stats") or {"s":0})["s"] or 0)
    if samples >= MIN_SAMPLES and (conf < 0.55 or gap < GAP_MIN):
        return None, 0.0, samples, post

    return best, conf, samples, post

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
    return True

def extract_seq_raw(text: str) -> Optional[str]:
    m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", text, flags=re.I)
    return m.group(1).strip() if m else None

def extract_after_num(text: str) -> Optional[int]:
    m = re.search(r"Entrar\s+ap[oó]s\s+o\s+([1-4])", text, flags=re.I)
    return int(m.group(1)) if m else None

def is_analise(text:str) -> bool:
    return bool(re.search(r"\bANALISANDO\b", text, flags=re.I))

GREEN_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bGREEN\b.*?\(([1-4])\)", re.I | re.S),
    re.compile(r"\bGREEN\b.*?([1-4])", re.I | re.S),
]
RED_PATTERNS = [
    re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\(([1-4])\)", re.I | re.S),
    re.compile(r"\bLOSS\b.*?([1-4])", re.I | re.S),
]

def extract_green_number(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in GREEN_PATTERNS:
        m = rx.search(t)
        if m: return int(re.findall(r"[1-4]", m.group(1))[0])
    return None

def extract_red_last_left(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in RED_PATTERNS:
        m = rx.search(t)
        if m: return int(re.findall(r"[1-4]", m.group(1))[0])
    return None

# =========================
# Mensagens (criativas)
# =========================
def _fmt_seq_conferencia(seen_csv: str, hit: int, _esperado: int) -> str:
    arr = [int(x) for x in (seen_csv or "").split(",") if x]
    arr = arr[-2:]
    parts = [str(n) for n in arr]
    parts.append(f"({int(hit)})")
    return " | ".join(parts) if parts else f"({int(hit)})"

async def send_green_imediato(sugerido:int, stage_txt:str="G0", seq_txt: str = ""):
    score = _score_line()
    txt = (
        f"✅ <b>GREEN!</b> em <b>{stage_txt}</b>\n"
        f"🎯 Número: <b>{sugerido}</b>\n"
        f"🚦 Sequência: {seq_txt}\n"
        f"📈 Placar: {score}"
    )
    await tg_broadcast(txt)

async def send_loss_imediato(sugerido:int, stage_txt:str="G2", obs: int | None = None, seq_txt: str = ""):
    score = _score_line()
    sufix = f" (saiu {int(obs)})" if obs is not None else ""
    txt = (
        f"❌ <b>LOSS</b> — {stage_txt}{sufix}\n"
        f"🎯 Nosso: <b>{sugerido}</b>\n"
        f"🚦 Observados: {seq_txt}\n"
        f"📉 Placar: {score}"
    )
    await tg_broadcast(txt)

async def _announce_stage_progress(stage: int, suggested: int, seen: str, last_obs: int):
    seq_txt = _fmt_seq_conferencia(seen, last_obs, suggested)
    if stage == 0:
        await tg_broadcast(
            f"🔁 <b>Indo para 1º gale (G1)</b>\n"
            f"🎯 Nosso: <b>{suggested}</b>\n"
            f"🚦 Observados: {seq_txt}"
        )
    elif stage == 1:
        await tg_broadcast(
            f"🔁 <b>Indo para 2º gale (G2)</b>\n"
            f"🎯 Nosso: <b>{suggested}</b>\n"
            f"🚦 Observados: {seq_txt}"
        )

# =========================
# Pendências (abrir/fechar) — VISÍVEL até G2
# =========================
def open_pending(suggested: int, source: str = "CHAN"):
    exec_write("""INSERT INTO pending_outcome
                  (created_at,strategy,suggested,stage,open,window_left,seen_numbers,announced,source)
                  VALUES (?,?,?,?,1,?, '', 0, ?)""", (now_ts(),"",int(suggested),0,MAX_STAGE,"CHAN" if not source else source))

async def close_pending_with_result(n_observed: int, event_kind: str):
    try:
        rows = query_all("""SELECT id, suggested, stage, window_left, seen_numbers, source
                            FROM pending_outcome WHERE open=1 ORDER BY id ASC""")
        if not rows: return {"ok": True, "no_open": True}

        for r in rows:
            pid        = r["id"]
            suggested  = int(r["suggested"])
            stage      = int(r["stage"])          # 0=G0, 1=G1, 2=G2
            left       = int(r["window_left"])
            seen       = (r["seen_numbers"] or "").strip()
            src        = (r["source"] or "CHAN").upper()
            seen_new   = (seen + ("," if seen else "") + str(int(n_observed)))

            if int(n_observed) == suggested:
                # GREEN em G0/G1/G2
                exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                           (seen_new, pid))
                seq_txt = _fmt_seq_conferencia(seen, n_observed, suggested)
                update_daily_score(stage, True)
                if src == "IA" and stage == 0:
                    update_daily_score_ia(0, True)
                await send_green_imediato(suggested, f"G{stage}", seq_txt=seq_txt)
            else:
                if left > 1:
                    # avança estágio
                    exec_write("""UPDATE pending_outcome
                                  SET stage=stage+1, window_left=window_left-1, seen_numbers=?
                                  WHERE id=?""", (seen_new, pid))
                    await _announce_stage_progress(stage, suggested, seen, n_observed)
                else:
                    # LOSS final em G2
                    exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                               (seen_new, pid))
                    update_daily_score(2, False)
                    if src == "IA":
                        update_daily_score_ia(0, False)
                    seq_txt = _fmt_seq_conferencia(seen, n_observed, suggested)
                    await send_loss_imediato(suggested, "G2", obs=n_observed, seq_txt=seq_txt)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# =========================
# IA FIRE-only (simples e robusta)
# =========================
_ia2_blocked_until_ts: int = 0
_ia2_last_fire_ts: int = 0
_ia2_sent_this_hour: int = 0
_ia2_hour_bucket: Optional[int] = None

def _ia2_hour_key() -> int: return int(datetime.now(timezone.utc).strftime("%Y%m%d%H"))
def _ia2_reset_hour():
    global _ia2_sent_this_hour, _ia2_hour_bucket
    hb = _ia2_hour_key()
    if _ia2_hour_bucket != hb: _ia2_hour_bucket = hb; _ia2_sent_this_hour = 0

def _adaptive_phase() -> str:
    # simples: usa amostras (ngrams) + “acima de mínimo” entra fase B/C
    samples = int((query_one("SELECT SUM(weight) AS s FROM ngram_stats") or {"s":0})["s"] or 0)
    if samples < 20_000: return "A"
    if samples < 100_000: return "B"
    return "C"

def _eff() -> dict:
    p = _adaptive_phase()
    if p == "A":
        return {"PHASE":"A","MAX_CONCURRENT_PENDINGS":2,"IA2_MAX_PER_HOUR":30,
                "IA2_MIN_SECONDS_BETWEEN_FIRE":7,"IA2_COOLDOWN_AFTER_LOSS":10,
                "IA2_TIER_STRICT":0.59,"IA2_DELTA_GAP":0.04,"IA2_GAP_SAFETY":IA2_GAP_SAFETY,
                "MIN_CONF_G0":0.52,"MIN_GAP_G0":0.035}
    elif p == "B":
        return {"PHASE":"B","MAX_CONCURRENT_PENDINGS":2,"IA2_MAX_PER_HOUR":20,
                "IA2_MIN_SECONDS_BETWEEN_FIRE":10,"IA2_COOLDOWN_AFTER_LOSS":12,
                "IA2_TIER_STRICT":0.60,"IA2_DELTA_GAP":0.04,"IA2_GAP_SAFETY":IA2_GAP_SAFETY,
                "MIN_CONF_G0":0.53,"MIN_GAP_G0":0.035}
    else:
        return {"PHASE":"C","MAX_CONCURRENT_PENDINGS":1,"IA2_MAX_PER_HOUR":12,
                "IA2_MIN_SECONDS_BETWEEN_FIRE":12,"IA2_COOLDOWN_AFTER_LOSS":15,
                "IA2_TIER_STRICT":0.62,"IA2_DELTA_GAP":0.03,"IA2_GAP_SAFETY":IA2_GAP_SAFETY,
                "MIN_CONF_G0":0.55,"MIN_GAP_G0":0.040}

def _has_open_pending() -> bool:
    eff = _eff()
    open_cnt = int((query_one("SELECT COUNT(*) AS c FROM pending_outcome WHERE open=1") or {"c":0})["c"])
    return open_cnt >= eff["MAX_CONCURRENT_PENDINGS"]

def _ia2_blocked_now() -> bool: return now_ts() < _ia2_blocked_until_ts
def _ia_set_post_loss_block():
    global _ia2_blocked_until_ts
    _ia2_blocked_until_ts = now_ts() + int(_eff()["IA2_COOLDOWN_AFTER_LOSS"])

def _ia2_antispam_ok() -> bool:
    _ia2_reset_hour()
    return _ia2_sent_this_hour < int(_eff()["IA2_MAX_PER_HOUR"])

def _ia2_can_fire_now() -> bool:
    eff = _eff()
    if _ia2_blocked_now(): return False
    if not _ia2_antispam_ok(): return False
    if now_ts() - _ia2_last_fire_ts < int(eff["IA2_MIN_SECONDS_BETWEEN_FIRE"]): return False
    return True

def _ia2_mark_fire_sent():
    global _ia2_last_fire_ts, _ia2_sent_this_hour
    _ia2_last_fire_ts = now_ts(); _ia2_sent_this_hour += 1

async def ia2_process_once():
    if _has_open_pending(): return
    if not _ia2_can_fire_now(): return
    # GEN: base=1..4 sem after
    best, conf, samples, post = suggest_number([1,2,3,4], after_num=None)
    if best is None: return
    # gap checado dentro do suggest + filtros por samples
    open_pending(int(best), source="IA")
    await tg_broadcast(f"🤖 <b>Entrada Confirmada (IA)</b>\n⚡ Número seco (G0): <b>{best}</b>\n📊 Conf: {conf*100:.2f}% | Amostra≈{samples}")
    _ia2_mark_fire_sent()

# =========================
# Rotas
# =========================
@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>"}

class Update(BaseModel):
    update_id: int
    channel_post: Optional[dict] = None
    message: Optional[dict] = None
    edited_channel_post: Optional[dict] = None
    edited_message: Optional[dict] = None

@app.on_event("startup")
async def _boot_tasks():
    async def _ia_loop():
        while True:
            try:
                await ia2_process_once()
            except Exception as e:
                print("[IA] error:", e)
            await asyncio.sleep(2.0)
    asyncio.create_task(_ia_loop())

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN: raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg: return {"ok": True}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    t = re.sub(r"\s+", " ", text)

    # GREEN/RED observados
    gnum = extract_green_number(t)
    redn = extract_red_last_left(t)
    if gnum is not None or redn is not None:
        n_observed = gnum if gnum is not None else redn
        append_timeline(n_observed); update_ngrams()
        return await close_pending_with_result(n_observed, "GREEN" if gnum is not None else "RED")

    # ANALISANDO -> alimenta timeline
    if is_analise(t):
        seq_raw = extract_seq_raw(t)
        if seq_raw:
            parts = re.findall(r"[1-4]", seq_raw)
            seq = [int(x) for x in parts][::-1]  # mais antigo -> mais novo
            for n in seq: append_timeline(n)
            update_ngrams()
        return {"ok": True, "analise": True}

    # ENTRADA (abre pendência + publica número G0)
    if is_real_entry(t):
        # Base/after simples: GEN + after se existir
        after = extract_after_num(t)
        best, conf, samples, _ = suggest_number([1,2,3,4], after_num=after)
        if best is None: return {"ok": True, "skipped_low_conf": True}
        open_pending(int(best), source="CHAN")
        aft_txt = f" após {after}" if after else ""
        await tg_broadcast(
            f"🎯 <b>Entrada Confirmada</b>\n"
            f"⚡ Número seco (G0): <b>{best}</b>\n"
            f"🧩 Padrão: GEN{aft_txt}\n"
            f"📊 Confiança: {conf*100:.2f}% | Amostra≈{samples}"
        )
        return {"ok": True, "sent": True, "number": int(best), "conf": float(conf), "samples": int(samples)}

    return {"ok": True, "skipped": True}

# =========================
# Debug/Health
# =========================
def _dir_size_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try: total += os.path.getsize(fp)
            except: pass
    return total

@app.get("/health")
async def health():
    g0,g1,g2,loss,streak,greens,total,acc = _daily_score_snapshot()
    return {
        "ok": True,
        "utc": utc_iso(),
        "db": DB_PATH,
        "phase": _adaptive_phase(),
        "score": {"greens": greens, "loss": loss, "acc_pct": round(acc,2),
                  "g0": g0, "g1": g1, "g2": g2, "streak": streak},
        "intel_dir_size": _dir_size_bytes(INTEL_DIR)
    }

@app.get("/debug/state")
async def debug_state():
    row = query_one("SELECT SUM(weight) AS s FROM ngram_stats")
    samples = int((row["s"] or 0) if row else 0)
    eff = _eff()
    return {
        "samples": samples,
        "phase": eff["PHASE"],
        "cfg": eff,
        "antispam_hour_sent": _ia2_sent_this_hour,
        "cooldown_left": max(0, (_ia2_blocked_until_ts or 0) - now_ts()),
        "last_fire_age": max(0, now_ts() - (_ia2_last_fire_ts or 0)),
        "score_today": {
            "greens_total": _daily_score_snapshot()[5],
            "loss": _daily_score_snapshot()[3],
            "acc_pct": round(_daily_score_snapshot()[7],2),
        }
    }

_last_flush_ts = 0
@app.get("/debug/flush")
async def debug_flush(key: str = "", days: int = 7):
    global _last_flush_ts
    if key != FLUSH_KEY: return {"ok": False, "error": "unauthorized"}
    now = now_ts()
    if now - _last_flush_ts < 60:
        return {"ok": False, "error": "flush_cooldown", "retry_after_seconds": 60 - (now - _last_flush_ts)}
    g0,g1,g2,loss,streak,greens,total,acc = _daily_score_snapshot()
    await tg_broadcast(
        "🩺 <b>Saúde do Guardião</b>\n"
        f"⏱️ UTC: <code>{utc_iso()}</code>\n"
        f"📊 Placar (hoje): GREEN={greens} • LOSS={loss} • Acerto={acc:.2f}%\n"
        f"↳ Detalhe: G0={g0} G1={g1} G2={g2} | 🔥 Streak={streak}"
    )
    _last_flush_ts = now
    return {"ok": True, "flushed": True}