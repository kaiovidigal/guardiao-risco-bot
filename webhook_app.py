# -*- coding: utf-8 -*-
# Fan Tan — Guardião (G0 + Recuperação G1/G2) — IA por Cauda(40) + Contexto(20)
#
# Destaques:
# - Sem n-gramas/bigrama: a IA decide usando
#     (a) frequência da cauda dos últimos 40 resultados e
#     (b) repetição de contexto: procura ocorrências do MESMO contexto dos últimos 20 números
#         no histórico (scan em memória com CAP de 20 matches) e soma as estatísticas do
#         "próximo número" que veio naquelas vezes.
# - GREEN/RED: salva o ÚLTIMO número entre parênteses. ANALISANDO só registra sequência.
# - Recuperação G1/G2 explícita: mensagens "GREEN (recuperação G1/G2)" e placar separando G0/G1/G2/Loss.
# - Placar automático a cada 30 minutos.
# - Sinal próprio vai DIRETO ao canal réplica (não replica texto do canal de origem).
#
# Rotas principais:
#   POST /webhook/<WEBHOOK_TOKEN>
#   GET  /debug/state, /debug/reason, /debug/samples, /debug/flush
#
# ENV obrigatórias: TG_BOT_TOKEN, WEBHOOK_TOKEN
# ENV opcionais:   REPL_CHANNEL (-100...), INTEL_ANALYZE_INTERVAL (default 2s), DB_PATH (/data/data.db)

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
DB_PATH = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
REPL_CHANNEL   = os.getenv("REPL_CHANNEL", "-1003052132833").strip()  # canal réplica
SELF_LABEL_IA  = os.getenv("SELF_LABEL_IA", "Tiro seco por IA").strip()

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN e WEBHOOK_TOKEN via ENV.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
INTEL_ANALYZE_INTERVAL = float(os.getenv("INTEL_ANALYZE_INTERVAL", "2"))

# Janelas e caps
WINDOW_TAIL = 400        # quantos últimos puxar do DB para análise geral
TAIL_K      = 40         # cauda usada na frequência
CTX_K       = 20         # tamanho do contexto para "repetição de padrão"
MAX_CTX_MATCHES = 20     # CAP de matches de contexto (para não demorar)
MIN_SAMPLES = 500        # mínimo de amostras para começar a disparar com confiança
MAX_STAGE   = 3          # G0, G1, G2

# thresholds simples (podem endurecer com tempo)
MIN_CONF_INIT = 0.00     # começa em 0%: vai aprendendo e subindo
MIN_GAP_INIT  = 0.00
MIN_CONF_CAP  = 0.68     # teto de endurecimento
MIN_GAP_CAP   = 0.06

# antispam básico
IA_MAX_PER_HOUR            = 20
IA_MIN_SECONDS_BETWEEN     = 7
IA_COOLDOWN_AFTER_LOSS     = 10

app = FastAPI(title="Fantan Guardião — IA por Cauda+Contexto", version="4.0.0")

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
    cur.execute("""CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
        suggested INTEGER NOT NULL, stage INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'IA')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_outcome (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, suggested INTEGER NOT NULL,
        stage INTEGER NOT NULL, open INTEGER NOT NULL, window_left INTEGER NOT NULL,
        seen_numbers TEXT DEFAULT '', announced INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'IA')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY, g0 INTEGER NOT NULL DEFAULT 0, g1 INTEGER NOT NULL DEFAULT 0,
        g2 INTEGER NOT NULL DEFAULT 0, loss INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0)""")
    con.commit(); con.close()

init_db()

# =========================
# Utils / Telegram
# =========================
def now_ts() -> int: return int(time.time())
def utc_iso() -> str: return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
def today_key() -> str: return datetime.now(timezone.utc).strftime("%Y%m%d")

async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True})

async def broadcast(text: str):
    if REPL_CHANNEL: await tg_send_text(REPL_CHANNEL, text, "HTML")

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
    # precisa ter algum contexto
    return any(re.search(p, t, flags=re.I) for p in [
        r"Sequ[eê]ncia:\s*[\d\s\|\-]+",
        r"\bKWOK\s*[1-4]\s*-\s*[1-4]",
        r"\bSS?H\s*[1-4](?:-[1-4]){0,3}",
        r"\bODD\b|\bEVEN\b",
        r"Entrar\s+ap[oó]s\s+o\s+[1-4]"
    ])

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

def extract_green(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in GREEN_PATTERNS:
        m = rx.search(t)
        if m:
            g = m.group(1) if m.lastindex else None
            return _last_num_in_group(g) if g is not None else _last_num_in_group(m.group(0))
    return None

def extract_red(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in RED_PATTERNS:
        m = rx.search(t)
        if m:
            g = m.group(1) if m.lastindex else None
            return _last_num_in_group(g) if g is not None else _last_num_in_group(m.group(0))
    return None

def is_analise(text: str) -> bool:
    return bool(re.search(r"\bANALISANDO\b", text, flags=re.I))

def extract_sequence(text: str) -> List[int]:
    m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", text, flags=re.I)
    if not m: return []
    parts = re.findall(r"[1-4]", m.group(1))
    return [int(x) for x in parts]

# =========================
# Timeline helpers
# =========================
def append_timeline(n: int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_tail(window:int=WINDOW_TAIL) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

# =========================
# Scorer Cauda(40) + Contexto(20)
# =========================
def _post_from_tail(tail: List[int]) -> Dict[int, float]:
    if not tail: return {1:0.25,2:0.25,3:0.25,4:0.25}
    k = min(TAIL_K, len(tail))
    lastk = tail[-k:]
    c = Counter(lastk)
    tot = sum(c.get(i,0) for i in [1,2,3,4]) or 1
    return {i: c.get(i,0)/tot for i in [1,2,3,4]}

def _ctx20_matches_next_freq(all_series: List[int], ctx: List[int], cap:int=MAX_CTX_MATCHES) -> Dict[int,int]:
    """Procura até cap ocorrências do contexto ctx no histórico e coleta o 'próximo' número após cada ocorrência."""
    if not all_series or not ctx or len(all_series) <= len(ctx): return {}
    ctxlen = len(ctx)
    found = 0
    freq = {1:0,2:0,3:0,4:0}
    # percorre o histórico até o penúltimo possível
    for i in range(0, len(all_series) - ctxlen):
        if all_series[i:i+ctxlen] == ctx:
            nxt_idx = i + ctxlen
            if nxt_idx < len(all_series):
                nxt = all_series[nxt_idx]
                if nxt in (1,2,3,4):
                    freq[nxt] += 1
                    found += 1
                    if found >= cap: break
    return freq

def _combine_posts(post_tail: Dict[int,float], freq_ctx: Dict[int,int]) -> Dict[int,float]:
    # transforma freq_ctx em prob (Laplace suave)
    tot_ctx = sum(freq_ctx.values())
    p_ctx = {i: (freq_ctx.get(i,0) + 1) / (tot_ctx + 4) for i in [1,2,3,4]}
    # combina com pesos (cauda dominante, contexto como reforço)
    # se houver ctx forte, ele empurra o ranking
    scores = {}
    for i in [1,2,3,4]:
        scores[i] = (post_tail.get(i,0.0) ** 1.0) * (p_ctx.get(i,0.25) ** 1.0)
    total = sum(scores.values()) or 1e-9
    return {i: scores[i]/total for i in [1,2,3,4]}

def _best_with_gap(post: Dict[int,float]) -> Tuple[Optional[int], float, float]:
    a = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a: return None, 0.0, 0.0
    if len(a) == 1: return a[0][0], a[0][1], 1.0
    gap = a[0][1] - a[1][1]
    return a[0][0], a[0][1], gap

# thresholds dinâmicos simples baseados em volume
def _dyn_thresholds(samples:int) -> Tuple[float,float]:
    # começa em zero e sobe suavemente até os CAPs conforme ganhamos histórico
    if samples < 2_000:
        return (MIN_CONF_INIT, MIN_GAP_INIT)
    # cresce linearmente até 100k
    x = min(1.0, (samples - 2_000) / 98_000.0)
    conf = MIN_CONF_INIT + x * (MIN_CONF_CAP - MIN_CONF_INIT)
    gap  = MIN_GAP_INIT  + x * (MIN_GAP_CAP  - MIN_GAP_INIT)
    return (round(conf,3), round(gap,3))

# =========================
# Placar
# =========================
def _read_score() -> Tuple[int,int,int,int,int]:
    y = today_key()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    if not row: return 0,0,0,0,0
    return int(row["g0"] or 0), int(row["g1"] or 0), int(row["g2"] or 0), int(row["loss"] or 0), int(row["streak"] or 0)

def _write_score(g0:int,g1:int,g2:int,loss:int,streak:int):
    y = today_key()
    exec_write("""INSERT INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak)
                  VALUES (?,?,?,?,?,?)
                  ON CONFLICT(yyyymmdd) DO UPDATE SET
                    g0=excluded.g0, g1=excluded.g1, g2=excluded.g2,
                    loss=excluded.loss, streak=excluded.streak""",
               (y,g0,g1,g2,loss,streak))

async def _send_scoreboard():
    g0,g1,g2,loss,streak = _read_score()
    total = g0+g1+g2+loss
    acc = (100.0*(g0+g1+g2)/total) if total else 0.0
    txt = (f"📊 <b>Placar (30m)</b>\n"
           f"🟢 G0:<b>{g0}</b> | 🟢G1:<b>{g1}</b> | 🟢G2:<b>{g2}</b> | 🔴Loss:<b>{loss}</b>\n"
           f"✅ Acerto: <b>{acc:.2f}%</b> | 🔥 Streak: <b>{streak}</b>")
    await broadcast(txt)

# =========================
# Pendências e fechamento
# =========================
def open_pending(suggested:int, source:str="IA"):
    exec_write("""INSERT INTO pending_outcome (created_at, suggested, stage, open, window_left, source)
                  VALUES (?,?,?,?,?,?)""", (now_ts(), int(suggested), 0, 1, MAX_STAGE, source))

async def _close_with_result(n_observed:int):
    rows = query_all("""SELECT id, suggested, stage, window_left, seen_numbers, source
                        FROM pending_outcome WHERE open=1 ORDER BY id ASC""")
    if not rows: return
    for r in rows:
        pid, suggested, stage, left = r["id"], int(r["suggested"]), int(r["stage"]), int(r["window_left"])
        seen = (r["seen_numbers"] or "")
        seen_new = (seen + ("," if seen else "") + str(int(n_observed))).strip()

        if int(n_observed) == suggested:
            # GREEN
            exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                       (seen_new, pid))
            g0,g1,g2,loss,streak = _read_score()
            if stage == 0:
                g0 += 1; streak += 1
                await broadcast(f"✅ <b>GREEN</b> — Número: <b>{suggested}</b> (G0)")
            elif stage == 1:
                g1 += 1; streak += 1
                await broadcast(f"✅ <b>GREEN</b> — Número: <b>{suggested}</b> (recuperação G1)")
            else:
                g2 += 1; streak += 1
                await broadcast(f"✅ <b>GREEN</b> — Número: <b>{suggested}</b> (recuperação G2)")
            _write_score(g0,g1,g2,loss,streak)
        else:
            # não bateu
            if left > 1:
                # avança estágio e mantém aberta
                exec_write("""UPDATE pending_outcome
                              SET stage=stage+1, window_left=window_left-1, seen_numbers=? WHERE id=?""",
                           (seen_new, pid))
                if stage == 0:
                    # contabiliza LOSS no G0 quando sobe para G1
                    g0,g1,g2,loss,streak = _read_score()
                    loss += 1; streak = 0
                    _write_score(g0,g1,g2,loss,streak)
                    await broadcast(f"❌ <b>LOSS</b> — Número: <b>{suggested}</b> (G0)")
            else:
                # estourou janela (já contou loss no G0)
                exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                           (seen_new, pid))

# =========================
# IA (decisor por cauda+contexto)
# =========================
IA_SENT_THIS_HOUR = 0
IA_HOUR_BUCKET    = None
IA_LAST_FIRE_TS   = 0
IA_BLOCK_UNTIL    = 0
_last_reason = "—"
_last_reason_ts = 0

def _mark_reason(r:str):
    global _last_reason, _last_reason_ts
    _last_reason = r
    _last_reason_ts = now_ts()

def _hour_key() -> int:
    return int(datetime.now(timezone.utc).strftime("%Y%m%d%H"))

def _reset_hour():
    global IA_SENT_THIS_HOUR, IA_HOUR_BUCKET
    hb = _hour_key()
    if IA_HOUR_BUCKET != hb:
        IA_SENT_THIS_HOUR = 0
        IA_HOUR_BUCKET = hb

def _antispam_ok() -> bool:
    _reset_hour()
    return IA_SENT_THIS_HOUR < IA_MAX_PER_HOUR

def _mark_sent():
    global IA_SENT_THIS_HOUR
    IA_SENT_THIS_HOUR += 1

def _can_fire_now() -> bool:
    if now_ts() < (IA_BLOCK_UNTIL or 0): return False
    if not _antispam_ok(): return False
    if now_ts() - (IA_LAST_FIRE_TS or 0) < IA_MIN_SECONDS_BETWEEN: return False
    return True

def _post_tail_ctx(tail: List[int]) -> Tuple[Dict[int,float], int]:
    post_tail = _post_from_tail(tail)
    # contexto dos últimos CTX_K
    ctx = tail[-CTX_K:] if len(tail) >= CTX_K else []
    freq_ctx = _ctx20_matches_next_freq(tail, ctx, cap=MAX_CTX_MATCHES) if ctx else {}
    post = _combine_posts(post_tail, freq_ctx if freq_ctx else {1:0,2:0,3:0,4:0})
    return post, sum(freq_ctx.values()) if freq_ctx else 0

async def ia_once():
    global IA_LAST_FIRE_TS, IA_BLOCK_UNTIL
    tail = get_tail(WINDOW_TAIL)
    samples = len(tail)

    if samples < TAIL_K + 5:
        _mark_reason(f"amostra_insuficiente({samples})")
        return

    post, ctx_hits = _post_tail_ctx(tail)
    best, conf, gap = _best_with_gap(post)

    # thresholds dinâmicos via volume total
    min_conf, min_gap = _dyn_thresholds(samples)
    if not best:
        _mark_reason("sem_best")
        return

    if not _can_fire_now():
        _mark_reason("antispam/cooldown")
        return

    if samples >= MIN_SAMPLES and (conf >= min_conf and gap >= min_gap):
        # dispara
        open_pending(int(best), "IA")
        IA_LAST_FIRE_TS = now_ts()
        _mark_sent()
        await broadcast(
            f"🤖 <b>{SELF_LABEL_IA} [FIRE]</b>\n"
            f"🎯 G0: <b>{best}</b>\n"
            f"📊 Conf≈<b>{conf*100:.2f}%</b> | Gap≈<b>{gap*100:.2f}%</b> | Cauda({TAIL_K}) + Ctx20 hits=<b>{ctx_hits}</b>"
        )
        _mark_reason(f"FIRE(best={best},conf={conf:.3f},gap={gap:.3f},ctx_hits={ctx_hits})")
        return

    # exploração leve enquanto não bate threshold: escolhe o mais frequente da cauda(40)
    if _can_fire_now():
        freq_tail = _post_from_tail(tail)
        expl = max(freq_tail.items(), key=lambda kv: kv[1])[0]
        open_pending(int(expl), "IA")
        IA_LAST_FIRE_TS = now_ts()
        _mark_sent()
        await broadcast(
            f"🤖 <b>{SELF_LABEL_IA} [WARMUP]</b>\n"
            f"🎯 G0: <b>{expl}</b>\n"
            f"📊 Cauda({TAIL_K}) dominante. Ctx20 insuficiente para confiança."
        )
        _mark_reason(f"WARMUP_FIRE(expl={expl})")
        return

# =========================
# Health / Score loop
# =========================
async def _score_loop():
    while True:
        try:
            await _send_scoreboard()
        except Exception as e:
            print(f"[SCOREBOARD] erro: {e}")
        await asyncio.sleep(30*60)

async def _ia_loop():
    while True:
        try:
            await ia_once()
        except Exception as e:
            print(f"[IA] erro: {e}")
        await asyncio.sleep(max(0.2, INTEL_ANALYZE_INTERVAL))

# =========================
# Webhook & Debug
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
async def _boot():
    asyncio.create_task(_score_loop())
    asyncio.create_task(_ia_loop())

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

    # GREEN / RED vindos do canal
    g = extract_green(t)
    r = extract_red(t)
    if g is not None or r is not None:
        n = g if g is not None else r
        append_timeline(int(n))
        await _close_with_result(int(n))
        return {"ok": True, "observed": int(n)}

    # ANALISANDO — apenas registra sequência
    if is_analise(t):
        seq = extract_sequence(t)
        if seq:
            # registra do mais antigo para o mais novo
            for n in seq[::-1]:
                append_timeline(int(n))
        return {"ok": True, "analise": True}

    # ENTRADA CONFIRMADA — ignoramos (não replicamos) para manter só nosso sinal IA
    return {"ok": True, "skipped": True}

@app.get("/debug/state")
async def dbg_state():
    try:
        tail = get_tail(WINDOW_TAIL)
        post, ctx_hits = _post_tail_ctx(tail) if tail else ({1:0.25,2:0.25,3:0.25,4:0.25}, 0)
        best, conf, gap = _best_with_gap(post)
        min_conf, min_gap = _dyn_thresholds(len(tail))
        g0,g1,g2,loss,streak = _read_score()
        return {
            "samples": len(tail),
            "tail_k": TAIL_K,
            "ctx_k": CTX_K,
            "ctx_hits": ctx_hits,
            "post": post,
            "best": best, "conf": conf, "gap": gap,
            "min_conf": min_conf, "min_gap": min_gap,
            "score_today": {"g0":g0,"g1":g1,"g2":g2,"loss":loss,"streak":streak},
            "last_reason": _last_reason,
            "last_reason_age_s": max(0, now_ts()-(_last_reason_ts or now_ts()))
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/debug/reason")
async def dbg_reason():
    return {"last_reason": _last_reason, "age_seconds": max(0, now_ts()-(_last_reason_ts or now_ts()))}

@app.get("/debug/samples")
async def dbg_samples():
    return {"samples": _get_count_timeline()}

def _get_count_timeline() -> int:
    row = query_one("SELECT COUNT(*) AS c FROM timeline")
    return int((row["c"] or 0) if row else 0)

_last_flush = 0
@app.get("/debug/flush")
async def dbg_flush():
    global _last_flush
    if now_ts() - (_last_flush or 0) < 60:
        return {"ok": False, "error": "cooldown", "retry_after": 60 - (now_ts()-_last_flush)}
    try:
        await _send_scoreboard()
        tail = get_tail(WINDOW_TAIL)
        post, ctx_hits = _post_tail_ctx(tail) if tail else ({1:0.25,2:0.25,3:0.25,4:0.25}, 0)
        best, conf, gap = _best_with_gap(post)
        await broadcast(
            "🩺 <b>Health</b>\n"
            f"⏱️ UTC: <code>{utc_iso()}</code>\n"
            f"🧪 samples/timeline: <b>{len(tail)}</b>\n"
            f"🧠 tail({TAIL_K})+ctx({CTX_K}) hits={ctx_hits} • best={best} conf≈{(conf or 0)*100:.2f}% gap≈{(gap or 0)*100:.2f}%\n"
            f"ℹ️ { _last_reason }"
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    _last_flush = now_ts()
    return {"ok": True}
