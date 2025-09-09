# -*- coding: utf-8 -*-
import os, re, json, time, sqlite3, hashlib
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

if not TG_BOT_TOKEN or not PUBLIC_CHANNEL or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN, PUBLIC_CHANNEL e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# Hiperparâmetros do modelo
WINDOW = 200        # quantos últimos números usar na timeline
DECAY  = 0.97       # decaimento por recência (ngrams)
W4, W3, W2, W1 = 0.40, 0.30, 0.20, 0.10    # pesos do back-off
ALPHA, BETA, GAMMA = 1.00, 0.70, 0.30      # pesos (ngrams, padrão, estratégia)
TRIGGER_P   = 0.82   # alerta de padrão (sem Entrada Confirmada)
TRIGGER_SUP = 12     # suporte mínimo

COOLDOWN_S = 12      # anti-duplicata rápida por chat

app = FastAPI(title="Fantan Guardião — Número Seco", version="2.0.0")

# =========================
# DB
# =========================
DB_PATH = "data.db"

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()

    # timeline (linha do tempo)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        number INTEGER NOT NULL
    )""")

    # n-gram stats (com pesos acumulados)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ngram_stats (
        n INTEGER NOT NULL,
        ctx TEXT NOT NULL,
        next INTEGER NOT NULL,
        weight REAL NOT NULL,
        PRIMARY KEY (n, ctx, next)
    )""")

    # histórico por padrão (KWOK-2-3, SSH-3-2-1, ODD, EVEN, SEQ, ...)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stats_pattern (
        pattern_key TEXT NOT NULL,
        number INTEGER NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (pattern_key, number)
    )""")

    # histórico por estratégia (id)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stats_strategy (
        strategy TEXT NOT NULL,
        number INTEGER NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (strategy, number)
    )""")

    # sugestões enviadas (dedupe por message_id do canal)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS suggestions (
        source_msg_id INTEGER PRIMARY KEY,
        strategy TEXT,
        seq_raw TEXT,
        context_key TEXT,
        pattern_key TEXT,
        base TEXT,                 -- JSON dos candidatos usados
        suggested_number INTEGER,
        stage TEXT,                -- G0/G1/G2
        sent_at INTEGER
    )""")

    # ponte para casar resultado por estratégia
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

    # placar diário
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        streak INTEGER NOT NULL DEFAULT 0
    )""")

    # cooldown simples por chat
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cooldown (
        chat_id INTEGER PRIMARY KEY,
        last_ts REAL
    )""")

    con.commit()
    con.close()

init_db()

def now_ts() -> int:
    return int(time.time())

def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")

# =========================
# Utils Telegram
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
    return [r["number"] for r in rows][::-1]  # antigo -> recente

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
            ctx = tail[t-(n-1):t]  # n-1 itens
            ctx_key = ",".join(str(x) for x in ctx)
            con.execute("""
                INSERT INTO ngram_stats (n, ctx, next, weight)
                VALUES (?,?,?,?)
                ON CONFLICT(n, ctx, next) DO UPDATE SET weight = ngram_stats.weight + excluded.weight
            """, (n, ctx_key, int(nxt), float(w)))

def prob_from_ngrams(con: sqlite3.Connection, ctx: List[int], candidate: int) -> float:
    """P(candidate | ctx) proporcional ao peso; se não houver, retorna 0."""
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
# Filtros / Parsers
# =========================
MUST_HAVE = (r"ENTRADA\s+CONFIRMADA", r"Mesa:\s*Fantan\s*-\s*Evolution")
MUST_NOT  = (r"\bANALISANDO\b", r"\bPlacar do dia\b", r"\bAPOSTA ENCERRADA\b")

def is_real_entry(text: str) -> bool:
    t = re.sub(r"\s+", " ", text).strip()
    for bad in MUST_NOT:
        if re.search(bad, t, flags=re.I): return False
    for good in MUST_HAVE:
        if not re.search(good, t, flags=re.I): return False
    # precisa ter alguma condição (Sequência / SSH / KWOK / ODD/EVEN / "Entrar após")
    has_ctx = any(re.search(p, t, flags=re.I) for p in [
        r"Sequ[eê]ncia:\s*[\d\s\|\-]+",
        r"\bKWOK\s*\d\s*-\s*\d",
        r"\bSS?H\s*[\d\-]+",
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

def parse_bases_and_pattern(text: str) -> Tuple[List[int], str]:
    t = re.sub(r"\s+", " ", text).strip()

    # KWOK X-Y
    m = re.search(r"\bKWOK\s*([1-4])\s*-\s*([1-4])", t, flags=re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return [a,b], f"KWOK-{a}-{b}"

    # ODD / EVEN
    if re.search(r"\bODD\b", t, flags=re.I):
        return [1,3], "ODD"
    if re.search(r"\bEVEN\b", t, flags=re.I):
        return [2,4], "EVEN"

    # SSH A-B(-C)(-D)
    m = re.search(r"\bSS?H\s*([1-4])(?:-([1-4]))?(?:-([1-4]))?(?:-([1-4]))?", t, flags=re.I)
    if m:
        nums = [int(g) for g in m.groups() if g]
        return nums, "SSH-" + "-".join(str(x) for x in nums)

    # Sequência como fallback de base (pegamos distintos mais recentes)
    m = re.search(r"Sequ[eê]ncia:\s*([\d\s\|\-]+)", t, flags=re.I)
    if m:
        parts = re.findall(r"[1-4]", m.group(1))
        # LEMBRETE: no seu canal, a sequência é mostrada ESQUERDA=mais RECENTE.
        # Vamos reverter para cronologia antiga->recente antes de escolher base curta.
        seq_left_recent = [int(x) for x in parts]          # [L...R] (L=mais recente)
        seq_old_to_new  = seq_left_recent[::-1]            # cronologia
        # base = últimos distintos (máx 3) mantendo ordem cronológica
        seen = []
        for n in seq_old_to_new[::-1]:
            if n not in seen:
                seen.append(n)
            if len(seen) == 3:
                break
        bases = seen[::-1] if seen else []
        if bases:
            return bases, "SEQ"
    return [], "UNK"

GREEN_RE = re.compile(r"APOSTA\s+ENCERRADA.*?GREEN.*?\((\d)\)", re.I | re.S)
RED_NUM_RE = re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\((.*?)\)", re.I | re.S)  # ex: (2 | 4 | 2)

def extract_green_number(text: str) -> Optional[int]:
    m = GREEN_RE.search(text)
    return int(m.group(1)) if m else None

def extract_red_last_left(text: str) -> Optional[int]:
    """Se o canal publicar RED (...), pegamos o mais à esquerda (que para você é o mais RECENTE)."""
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
# Estatísticas (padrão/estratégia)
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

async def send_scoreboard(w:int,l:int,acc:float,streak:int):
    txt = (f"📊 <b>Placar do dia</b> — 🟢 {w} 🔴 {l}\n"
           f"✅ Acerto: {acc*100:.2f}%\n"
           f"🔥 Streak: {streak} GREEN(s)")
    await tg_send_text(PUBLIC_CHANNEL, txt)

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
    """
    Usa back-off de contextos terminando em 'after_num' (se fornecido).
    Se after_num for None, usa a cauda diretamente (últimos k-1).
    """
    score = 0.0
    if not tail:
        return 0.0

    # Encontrar a posição mais recente do gatilho 'after_num' na cauda
    # tail é antigo->recente; queremos a última ocorrência.
    if after_num is not None:
        idxs = [i for i,v in enumerate(tail) if v == after_num]
        if not idxs:
            # sem gatilho, usar os últimos contextos genéricos
            ctx4 = tail[-4:-0] if len(tail) >= 4 else []
            ctx3 = tail[-3:-0] if len(tail) >= 3 else []
            ctx2 = tail[-2:-0] if len(tail) >= 2 else []
            ctx1 = tail[-1:-0] if len(tail) >= 1 else []
        else:
            i = idxs[-1]
            # ctx termina no after_num (não inclui próximo)
            ctx1 = tail[max(0, i): i+1]                   # [after]
            ctx2 = tail[max(0, i-1): i+1] if i-1 >= 0 else []
            ctx3 = tail[max(0, i-2): i+1] if i-2 >= 0 else []
            ctx4 = tail[max(0, i-3): i+1] if i-3 >= 0 else []
    else:
        ctx4 = tail[-4:-0] if len(tail) >= 4 else []
        ctx3 = tail[-3:-0] if len(tail) >= 3 else []
        ctx2 = tail[-2:-0] if len(tail) >= 2 else []
        ctx1 = tail[-1:-0] if len(tail) >= 1 else []

    parts = []
    if len(ctx4)==4: parts.append((W4, prob_from_ngrams(con, ctx4[:-1], candidate)))
    if len(ctx3)==3: parts.append((W3, prob_from_ngrams(con, ctx3[:-1], candidate)))
    if len(ctx2)==2: parts.append((W2, prob_from_ngrams(con, ctx2[:-1], candidate)))
    if len(ctx1)==1: parts.append((W1, prob_from_ngrams(con, ctx1[:-1], candidate)))

    for w,p in parts:
        score += w * p
    return score

def laplace_ratio(wins:int, losses:int) -> float:
    return (wins + 1.0) / (wins + losses + 2.0)

def suggest_number(con: sqlite3.Connection, base: List[int], pattern_key: str, strategy: Optional[str], after_num: Optional[int]) -> Tuple[int,float,int,Dict[int,float]]:
    """
    Combina: prior (uniforme na base) × ngram_backoff^ALPHA × pattern^BETA × strategy^GAMMA.
    Retorna (numero, conf, samples, breakdown).
    """
    if not base:
        base = [1,2,3,4]

    tail = get_recent_tail(con, WINDOW)
    scores: Dict[int, float] = {}
    parts_dbg: Dict[int, Dict[str,float]] = {}

    # contagem de suporte aproximada (soma dos pesos disponíveis nos n-grams do maior contexto encontrado)
    samples = 0

    for c in base:
        # n-gram likelihood
        ng = ngram_backoff_score(con, tail, after_num, c)

        # padrão
        rowp = con.execute("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pattern_key, c)).fetchone()
        pw = rowp["wins"] if rowp else 0
        pl = rowp["losses"] if rowp else 0
        p_pat = laplace_ratio(pw, pl)

        # estratégia
        p_str = 1/len(base)
        if strategy:
            rows = con.execute("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?", (strategy, c)).fetchone()
            sw = rows["wins"] if rows else 0
            sl = rows["losses"] if rows else 0
            p_str = laplace_ratio(sw, sl)

        # prior uniforme dentro da base
        prior = 1.0/len(base)

        score = (prior) * ((ng or 1e-6) ** ALPHA) * (p_pat ** BETA) * (p_str ** GAMMA)
        scores[c] = score
        parts_dbg[c] = {"prior":prior, "ng":ng, "pat":p_pat, "str":p_str}

    # normaliza para confiança
    total = sum(scores.values()) or 1e-9
    post = {k: v/total for k,v in scores.items()}
    best = max(post.items(), key=lambda kv: kv[1])
    number, conf = best[0], best[1]

    # estimar "samples" como quantidade de pares de ngram utilizados
    roww = con.execute("SELECT SUM(weight) AS s FROM ngram_stats").fetchone()
    samples = int(roww["s"] or 0)

    return number, conf, samples, post

# =========================
# Mensagens
# =========================
def build_suggestion_msg(number:int, base:List[int], pattern_key:str, after_num:Optional[int], conf:float, samples:int, stage:str="G0") -> str:
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

@app.get("/debug/stats")
async def debug_stats():
    con = db()
    out = {
        "stats_pattern": [dict(r) for r in con.execute("SELECT * FROM stats_pattern LIMIT 200").fetchall()],
        "stats_strategy": [dict(r) for r in con.execute("SELECT * FROM stats_strategy LIMIT 200").fetchall()],
        "ngrams_total": con.execute("SELECT COUNT(*) c FROM ngram_stats").fetchone()["c"],
        "timeline_len": con.execute("SELECT COUNT(*) c FROM timeline").fetchone()["c"],
    }
    con.close()
    return out

@app.get("/debug/score")
async def debug_score():
    con = db()
    out = [dict(r) for r in con.execute("SELECT * FROM daily_score ORDER BY yyyymmdd DESC LIMIT 14").fetchall()]
    con.close()
    return out

@app.get("/debug/suggestions")
async def debug_suggestions():
    con = db()
    out = [dict(r) for r in con.execute("SELECT * FROM suggestions ORDER BY sent_at DESC LIMIT 20").fetchall()]
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

    # 1) GREEN / RED / GALE
    try:
        gnum = extract_green_number(t)
        gale_n = is_gale_info(t)
        red_last = extract_red_last_left(t)

        if gnum is not None or red_last is not None or gale_n is not None:
            # timeline: GREEN adiciona vencedor; RED com números adiciona o mais recente (à esquerda)
            if gnum is not None:
                append_timeline(con, gnum)
                update_ngrams(con)
            elif red_last is not None:
                append_timeline(con, red_last)
                update_ngrams(con)

            # aprendizado por estratégia
            strat = extract_strategy(t) or ""
            row = con.execute("SELECT suggested_number, context_key, pattern_key FROM last_by_strategy WHERE strategy=?", (strat,)).fetchone()
            won = False
            if row:
                suggested = row["suggested_number"]
                ctx_key   = row["context_key"]
                pat_key   = row["pattern_key"]
                if gnum is not None:
                    won = (int(suggested) == int(gnum))
                    bump_pattern(con, pat_key, suggested, won)
                    if strat:
                        bump_strategy(con, strat, suggested, won)
                    w,l,acc,streak = update_daily_score(con, won)
                    con.commit()
                    await send_scoreboard(w,l,acc,streak)
                else:
                    # Em gale info ou RED sem número, não alteramos placar,
                    # mas poderíamos recalcular G1/G2 se necessário.
                    pass

            con.commit()
            return {"ok": True, "learned_or_updated": True}
    finally:
        pass

    # 2) ANALISANDO -> alimentar timeline (NÃO enviar sugestão)
    if is_analise(t):
        # extrai a sequência e grava na cronologia correta
        seq_raw = extract_seq_raw(t)
        if seq_raw:
            parts = re.findall(r"[1-4]", seq_raw)
            # Seu canal: ESQUERDA = mais RECENTE → precisamos reverter para cronologia
            seq_left_recent = [int(x) for x in parts]
            seq_old_to_new  = seq_left_recent[::-1]
            for n in seq_old_to_new:
                append_timeline(con, n)
            update_ngrams(con)
            con.commit()
        con.close()
        return {"ok": True, "analise": True}

    # 3) ENTRADA CONFIRMADA -> 1 número seco
    if not is_real_entry(t):
        con.close()
        return {"ok": True, "skipped": True}

    # Dedupe por message_id
    if already_suggested(con, source_msg_id):
        con.close()
        return {"ok": True, "dup": True}

    strategy   = extract_strategy(t) or ""
    seq_raw    = extract_seq_raw(t) or ""
    after_num  = extract_after_num(t)
    base, pattern_key = parse_bases_and_pattern(t)

    # Se não veio base, ainda assim tentamos candidatos 1..4
    if not base:
        base = [1,2,3,4]
        pattern_key = "GEN"

    # cálculo
    number, conf, samples, post = suggest_number(con, base, pattern_key, strategy, after_num)

    # grava ponte e dedupe
    remember_suggestion(con, source_msg_id, strategy, seq_raw, "CTX", pattern_key, base, number, stage="G0")
    con.commit()
    con.close()

    # envia 1 única mensagem
    out = build_suggestion_msg(number, base, pattern_key, after_num, conf, samples, stage="G0")
    await tg_send_text(PUBLIC_CHANNEL, out)
    return {"ok": True, "sent": True}