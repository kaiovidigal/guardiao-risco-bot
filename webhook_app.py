# -*- coding: utf-8 -*-
# Fan Tan — Guardião (G0 + Recuperação G1/G2) — IA por Cauda(40) + Checagem Contexto(20)
#
# Destaques:
# - IA baseada apenas em frequência da cauda(40) + "contexto-20" (checagem rápida de recorrência)
# - Sem bigrama/trigrama: foco é velocidade e simplicidade estatística
# - Warm-up (exploração) com thresholds dinâmicos (sobem conforme aprendizado)
# - GREEN/RED: salva o ÚLTIMO número entre parênteses. ANALISANDO só registra sequência
# - Recuperação explícita G1/G2 com mensagens e placar separado (G0, G1, G2, Loss)
# - Placar automático a cada 30 minutos
# - Sinal próprio vai DIRETO ao canal réplica (não replica texto do canal de origem)
#
# Rotas úteis:
#   POST /webhook/<WEBHOOK_TOKEN>
#   GET  /debug/state, /debug/reason, /debug/samples, /debug/flush
#
# ENV necessários: TG_BOT_TOKEN, WEBHOOK_TOKEN, (opcional) PUBLIC_CHANNEL (se usar), REPL_CHANNEL
# Recomendações Render:
#   - Procfile: web: gunicorn webhook_app:app --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT

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
PUBLIC_CHANNEL = os.getenv("PUBLIC_CHANNEL", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
FLUSH_KEY      = os.getenv("FLUSH_KEY", "meusegredo123").strip()

# Canal de réplica (envio dos nossos sinais)
REPL_CHANNEL  = os.getenv("REPL_CHANNEL", "-1003052132833").strip()
REPL_ENABLED  = True if REPL_CHANNEL else False

if not TG_BOT_TOKEN or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# =========================
# MODO & Hiperparâmetros
# =========================
MAX_STAGE     = 3                 # G0 -> G1 -> G2
SCORE_G0_ONLY = False             # mostramos G0/G1/G2 separadamente no placar

WINDOW        = 400               # cauda geral para manter memória local/weights
TAIL_K        = 40                # janela curta para decisão do próximo
CTX20_SCAN    = 20                # tenta encontrar 20 ocorrências do padrão simples anterior
MIN_SAMPLES   = 1000              # mínimo de amostras ponderadas antes de confiar 100%

# Thresholds dinâmicos + warm-up
WARMUP_SAMPLES = 5_000
BASE_MIN_CONF  = 0.00   # começa liberado e sobe
BASE_MIN_GAP   = 0.000
CONF_MAX_CAP   = 0.999

# Anti-spam
IA_MAX_PER_HOUR            = 20
IA_MIN_SECONDS_BETWEEN_FIRE= 7
COOLDOWN_AFTER_LOSS        = 10

SELF_LABEL_IA = os.getenv("SELF_LABEL_IA", "Tiro seco por IA (Cauda40)")

# Sinalização INTEL (snapshot leve opcional)
INTEL_DIR = os.getenv("INTEL_DIR", "/var/data/ai_intel").rstrip("/")
os.makedirs(os.path.join(INTEL_DIR, "snapshots"), exist_ok=True)

app = FastAPI(title="Fantan Guardião — IA Cauda(40) + Recuperação G1/G2", version="4.0.0")

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
    cur.execute("""CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, suggested INTEGER NOT NULL,
        stage TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'IA')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending_outcome (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, suggested INTEGER NOT NULL,
        stage INTEGER NOT NULL, open INTEGER NOT NULL, window_left INTEGER NOT NULL, seen_numbers TEXT DEFAULT '',
        announced INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'IA')""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY, g0 INTEGER NOT NULL DEFAULT 0, g1 INTEGER NOT NULL DEFAULT 0,
        g2 INTEGER NOT NULL DEFAULT 0, loss INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0)""")
    con.commit()
    # colunas idempotentes
    for alter in [
        "ALTER TABLE pending_outcome ADD COLUMN seen_numbers TEXT DEFAULT ''",
        "ALTER TABLE pending_outcome ADD COLUMN announced INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE pending_outcome ADD COLUMN source TEXT NOT NULL DEFAULT 'IA'",
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
    async with httpx.AsyncClient(timeout=12) as client:
        await client.post(f"{TELEGRAM_API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True})

async def tg_broadcast(text: str, parse: str="HTML"):
    if REPL_ENABLED and REPL_CHANNEL:
        await tg_send_text(REPL_CHANNEL, text, parse)

async def send_green_stage(sugerido:int, stage:int):
    if stage==0: tag="G0"
    elif stage==1: tag="Recuperação G1"
    else: tag="Recuperação G2"
    await tg_broadcast(f"✅ <b>GREEN</b> — <b>{tag}</b> — Número: <b>{sugerido}</b>")

async def send_loss_g0(sugerido:int):
    await tg_broadcast(f"❌ <b>LOSS</b> — Número: <b>{sugerido}</b> (em G0)")

async def ia_send_signal(best:int, conf:float, tail_len:int, mode:str):
    conf = max(0.0, min(conf, CONF_MAX_CAP))
    txt = (f"🤖 <b>{SELF_LABEL_IA} [{mode}]</b>\n"
           f"🎯 Número seco (G0): <b>{best}</b>\n"
           f"📈 Conf: <b>{conf*100:.2f}%</b> | Amostra≈<b>{tail_len}</b>")
    await tg_broadcast(txt)

# =========================
# Timeline & n-grams (somente para contagem de amostras)
# =========================
DECAY = 0.985
def append_timeline(n: int):
    exec_write("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(n)))

def get_recent_tail(window: int = WINDOW) -> List[int]:
    rows = query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,))
    return [r["number"] for r in rows][::-1]

def update_ngrams(decay: float = DECAY, max_n: int = 2, window: int = WINDOW):
    # Mantém uma noção de "amostras" (peso) para thresholds dinâmicos, sem usar bigrama na predição final
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

def current_samples() -> int:
    row = query_one("SELECT SUM(weight) AS s FROM ngram_stats")
    return int((row["s"] or 0) if row else 0)

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

# GREEN/RED — pega o ÚLTIMO número entre parênteses; em "Número: X" pega direto
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
            g = m.group(1) if (m.lastindex and m.lastindex>=1) else None
            return _last_num_in_group(g) if g is not None else _last_num_in_group(m.group(0))
    return None

def extract_red_last_left(text: str) -> Optional[int]:
    t = re.sub(r"\s+", " ", text)
    for rx in RED_PATTERNS:
        m = rx.search(t)
        if m:
            g = m.group(1) if (m.lastindex and m.lastindex>=1) else None
            return _last_num_in_group(g) if g is not None else _last_num_in_group(m.group(0))
    return None

def is_analise(text:str) -> bool:
    return bool(re.search(r"\bANALISANDO\b", text, flags=re.I))

def extract_seq_raw(text: str) -> Optional[str]:
    m = re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)", text, flags=re.I)
    return m.group(1).strip() if m else None

# =========================
# Placar e métricas
# =========================
def update_daily_score(stage: int, won: bool):
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0,g1,g2,loss,streak = (row["g0"],row["g1"],row["g2"],row["loss"],row["streak"]) if row else (0,0,0,0,0)
    if won:
        if stage==0: g0 += 1
        elif stage==1: g1 += 1
        else: g2 += 1
        streak += 1
    else:
        loss  += 1
        streak = 0
    exec_write("""INSERT INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,?,?,?,?,?)
                  ON CONFLICT(yyyymmdd) DO UPDATE SET
                  g0=excluded.g0,g1=excluded.g1,g2=excluded.g2,loss=excluded.loss,streak=excluded.streak""",
               (y,g0,g1,g2,loss,streak))
    total = g0 + g1 + g2 + loss
    acc = ((g0+g1+g2)/total) if total else 0.0
    return g0,g1,g2,loss,acc,streak

def _score_text():
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0 = row["g0"] if row else 0
    g1 = row["g1"] if row else 0
    g2 = row["g2"] if row else 0
    loss = row["loss"] if row else 0
    streak = row["streak"] if row else 0
    total = g0 + g1 + g2 + loss
    acc = ((g0+g1+g2)/total*100.0) if total else 0.0
    return (f"📊 <b>Placar (30m)</b>\n"
            f"🟢 G0:<b>{g0}</b> • G1:<b>{g1}</b> • G2:<b>{g2}</b> • 🔴 Loss:<b>{loss}</b>\n"
            f"✅ Acerto: <b>{acc:.2f}%</b> • 🔥 Streak: <b>{streak}</b>")

async def _score_task():
    while True:
        try:
            await tg_broadcast(_score_text())
        except Exception as e:
            print(f"[SCORE] erro ao enviar: {e}")
        await asyncio.sleep(30*60)

# =========================
# Cauda(40) + Contexto(20)
# =========================
def tail_top2_freq(tail: List[int], k:int=40) -> Tuple[Dict[int,float], List[Tuple[int,int]]]:
    if not tail: 
        return ({1:0.25,2:0.25,3:0.25,4:0.25}, [])
    t = tail[-k:] if len(tail)>=k else tail[:]
    c = Counter(t)
    total = len(t)
    post = {n: (c.get(n,0)/total) for n in [1,2,3,4]}
    rank = sorted(((n,c.get(n,0)) for n in [1,2,3,4]), key=lambda x: x[1], reverse=True)
    return post, rank

def context20_match_boost(tail: List[int], candidate:int) -> float:
    """Procura sequências anteriores onde o top1 era igual ao candidate
       usando janelas de 20 elementos (rápido) — dá boost leve se recorrente."""
    if not tail or len(tail) < 25: 
        return 1.00
    ctx = tail[-20:]  # contexto atual
    rows = query_all("SELECT number FROM timeline ORDER BY id ASC")  # ordem cronológica
    seq = [r["number"] for r in rows]
    if len(seq) < 50: 
        return 1.00
    # varre com passo 1 buscando matches parciais (não precisa ser identico 100%)
    hits = 0
    for i in range(0, len(seq)-21):
        win = seq[i:i+20]
        if sum(1 for a,b in zip(win, ctx) if a==b) >= 12:  # 60% de similaridade rápida
            nxt = seq[i+20]  # próximo após aquela janela
            if nxt == candidate:
                hits += 1
                if hits >= CTX20_SCAN:
                    break
    if hits >= 10:   # recorrente forte
        return 1.06
    if hits >= 5:
        return 1.03
    if hits >= 2:
        return 1.015
    return 1.00

def pick_from_tail(tail: List[int]) -> Tuple[Optional[int], float, float, int, Dict[int,float]]:
    """Retorna: (numero, conf, gap, tail_len, posteriors)"""
    post, rank = tail_top2_freq(tail, k=TAIL_K)
    if not post: 
        return None, 0.0, 0.0, len(tail or []), {1:0.25,2:0.25,3:0.25,4:0.25}
    # aplica boost por recorrência de contexto-20
    boosted = {}
    for n, p in post.items():
        boosted[n] = p * context20_match_boost(tail, n)
    total = sum(boosted.values()) or 1e-9
    post2 = {k: v/total for k,v in boosted.items()}
    top = sorted(post2.items(), key=lambda kv: kv[1], reverse=True)
    if not top: 
        return None, 0.0, 0.0, len(tail), post2
    if len(top)==1:
        return top[0][0], top[0][1], top[0][1], len(tail), post2
    best, p1 = top[0]
    p2 = top[1][1]
    gap = p1 - p2
    return best, p1, gap, len(tail), post2

def dynamic_thresholds(samples:int) -> Tuple[float,float]:
    if samples < WARMUP_SAMPLES:
        return BASE_MIN_CONF, BASE_MIN_GAP
    # endurece com aprendizado (amostras) — suave
    # conf cresce até ~0.68; gap até ~0.06
    conf = min(0.68, 0.50 + min(0.12, (samples/200_000.0)*0.12))
    gap  = min(0.06, 0.030 + min(0.03, (samples/100_000.0)*0.03))
    return round(conf,3), round(gap,3)

# =========================
# Pendências e fechamento (G0/G1/G2)
# =========================
def open_pending(suggested: int, source: str = "IA"):
    exec_write("""INSERT INTO pending_outcome (created_at, suggested, stage, open, window_left, seen_numbers, announced, source)
                  VALUES (?,?,?,?,?,?,?,?)""",
               (now_ts(), int(suggested), 0, 1, MAX_STAGE, '', 1, source))

async def close_pending_with_result(n_observed: int):
    rows = query_all("""SELECT id, suggested, stage, window_left, seen_numbers, source
                        FROM pending_outcome WHERE open=1 ORDER BY id ASC""")
    if not rows: 
        return
    for r in rows:
        pid, suggested, stage, left = r["id"], int(r["suggested"]), int(r["stage"]), int(r["window_left"])
        seen_new = ((r["seen_numbers"] or "") + ("," if (r["seen_numbers"] or "") else "") + str(int(n_observed))).strip()
        if int(n_observed) == suggested:
            # GREEN no estágio atual
            exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?", (seen_new, pid))
            update_daily_score(stage, True)
            await send_green_stage(suggested, stage)
        else:
            # não bateu
            if left > 1:
                exec_write("""UPDATE pending_outcome SET stage=stage+1, window_left=window_left-1, seen_numbers=? WHERE id=?""",
                           (seen_new, pid))
                if stage == 0:
                    # loss no G0
                    update_daily_score(0, False)
                    await send_loss_g0(suggested)
            else:
                # esgotou G2 (já contou um loss no G0 caso tenha ocorrido no primeiro erro)
                exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",
                           (seen_new, pid))

# =========================
# IA loop — rápido e objetivo
# =========================
IA_SENT_THIS_HOUR=0; IA_HOUR_BUCKET=None; IA_LAST_FIRE_TS=0; IA_BLOCK_UNTIL=0
def _hour_key() -> int: return int(datetime.now(timezone.utc).strftime("%Y%m%d%H"))
def _reset_hour():
    global IA_SENT_THIS_HOUR, IA_HOUR_BUCKET
    hb=_hour_key()
    if IA_HOUR_BUCKET!=hb: IA_HOUR_BUCKET=hb; IA_SENT_THIS_HOUR=0
def _antispam_ok() -> bool:
    _reset_hour(); return IA_SENT_THIS_HOUR < IA_MAX_PER_HOUR
def _mark_sent():
    global IA_SENT_THIS_HOUR; IA_SENT_THIS_HOUR += 1
def _blocked_now() -> bool: return now_ts() < IA_BLOCK_UNTIL

def intel_snapshot(num:int, mode:str):
    try:
        path = os.path.join(INTEL_DIR, "snapshots", "latest_top.json")
        with open(path, "w") as f: json.dump({"ts": now_ts(), "suggested": num, "mode": mode}, f)
    except Exception as e: 
        print(f"[INTEL] snapshot error: {e}")

_last_reason, _last_reason_ts = "—", 0
def mark_reason(r: str):
    global _last_reason, _last_reason_ts
    _last_reason, _last_reason_ts = r, now_ts()

async def ia_step_once():
    global IA_LAST_FIRE_TS, IA_BLOCK_UNTIL
    tail = get_recent_tail(WINDOW)
    best, conf_raw, gap, tail_len, post = pick_from_tail(tail)
    samples = current_samples()

    # Antispam/cooldown
    if _blocked_now():
        mark_reason("cooldown_pos_loss")
        return
    if not _antispam_ok():
        mark_reason("limite_hora_atingido")
        return
    if now_ts() - (IA_LAST_FIRE_TS or 0) < IA_MIN_SECONDS_BETWEEN_FIRE:
        mark_reason("espacamento_minimo")
        return

    # Thresholds dinâmicos
    min_conf, min_gap = dynamic_thresholds(samples)

    # Caso 1: conf/gap suficientes + amostra mínima
    if best is not None and conf_raw >= min_conf and gap >= min_gap and samples >= MIN_SAMPLES:
        open_pending(int(best), source="IA")
        await ia_send_signal(int(best), conf_raw, tail_len, "FIRE")
        intel_snapshot(int(best), "FIRE")
        IA_LAST_FIRE_TS = now_ts(); _mark_sent()
        mark_reason(f"FIRE(best={best}, conf={conf_raw:.3f}, gap={gap:.3f}, tail={tail_len})")
        return

    # Caso 2: Warm-up (exploração) enquanto aprende
    if samples < WARMUP_SAMPLES:
        # escolhe top-1 de frequência simples
        top = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
        cand = int(top[0][0]) if top else 1
        open_pending(cand, source="IA")
        await ia_send_signal(cand, 0.0, tail_len, "WARMUP")
        intel_snapshot(cand, "WARMUP")
        IA_LAST_FIRE_TS = now_ts(); _mark_sent()
        mark_reason(f"WARMUP_FIRE(num={cand}, samples={samples})")
        return

    # Caso 3: Não enviar
    if samples < MIN_SAMPLES:
        mark_reason(f"amostra_insuficiente({samples}<{MIN_SAMPLES})")
    else:
        mark_reason(f"reprovado(conf={conf_raw:.3f}, gap={gap:.3f}, min=({min_conf:.3f},{min_gap:.3f}))")

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
    async def _ia_loop():
        while True:
            try: 
                await ia_step_once()
            except Exception as e:
                print(f"[IA] step error: {e}")
            await asyncio.sleep(1.5)  # ritmo rápido e estável
    try:
        asyncio.create_task(_ia_loop())
        asyncio.create_task(_score_task())
    except Exception as e:
        print(f"[STARTUP] error: {e}")

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN: 
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    upd = Update(**data)
    msg = upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg: 
        return {"ok": True}

    text = (msg.get("text") or msg.get("caption") or "").strip()
    t = re.sub(r"\s+", " ", text)

    # 1) GREEN / RED — salva último número e fecha pendências
    gnum = extract_green_number(t)
    redn = extract_red_last_left(t)
    if gnum is not None or redn is not None:
        n_observed = gnum if gnum is not None else redn
        append_timeline(n_observed)
        update_ngrams()
        await close_pending_with_result(n_observed)
        return {"ok": True, "observed": n_observed}

    # 2) ANALISANDO — apenas registra sequência, sem sinal
    if is_analise(t):
        seq_raw = extract_seq_raw(t)
        if seq_raw:
            parts = re.findall(r"[1-4]", seq_raw)
            seq = [int(x) for x in parts]
            # adiciona na ordem apresentada (normalmente esquerda→direita = do mais antigo ao mais novo)
            for n in seq:
                append_timeline(n)
            update_ngrams()
        return {"ok": True, "analise": True}

    # 3) ENTRADA CONFIRMADA — aqui não replicamos; IA decide por conta própria
    if is_real_entry(t):
        # IA não depende do texto do canal; apenas mantém seu próprio loop
        return {"ok": True, "entry_seen": True}

    return {"ok": True, "skipped": True}

# =========================
# DEBUG endpoints
# =========================
@app.get("/debug/samples")
async def debug_samples():
    smp = current_samples()
    return {"samples": smp, "MIN_SAMPLES": MIN_SAMPLES, "WARMUP_SAMPLES": WARMUP_SAMPLES}

@app.get("/debug/reason")
async def debug_reason():
    age = max(0, now_ts() - (_last_reason_ts or now_ts()))
    return {"last_reason": _last_reason, "last_reason_age_seconds": age}

@app.get("/debug/state")
async def debug_state():
    y = today_key_local()
    row = query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0 = row["g0"] if row else 0
    g1 = row["g1"] if row else 0
    g2 = row["g2"] if row else 0
    loss = row["loss"] if row else 0
    streak = row["streak"] if row else 0
    smp = current_samples()
    return {
        "placar": {"g0": g0, "g1": g1, "g2": g2, "loss": loss, "streak": streak},
        "samples": smp,
        "warmup": smp < WARMUP_SAMPLES,
        "anti_spam": {"sent_this_hour": IA_SENT_THIS_HOUR, "hour_bucket": _hour_key(),
                      "cooldown_after_loss": COOLDOWN_AFTER_LOSS, "min_between_fire": IA_MIN_SECONDS_BETWEEN_FIRE,
                      "max_per_hour": IA_MAX_PER_HOUR},
        "last_reason": _last_reason
    }

_last_flush_ts=0
@app.get("/debug/flush")
async def debug_flush(days: int = 7, key: str = ""):
    global _last_flush_ts
    if not key or key != FLUSH_KEY:
        return {"ok": False, "error": "unauthorized"}
    now = now_ts()
    if now - (_last_flush_ts or 0) < 60:
        return {"ok": False, "error": "flush_cooldown", "retry_after_seconds": 60 - (now - (_last_flush_ts or 0))}
    try:
        await tg_broadcast(_score_text())
    except Exception as e:
        print(f"[FLUSH] score err: {e}")
    _last_flush_ts = now
    return {"ok": True, "flushed": True}
