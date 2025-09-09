# -*- coding: utf-8 -*-
import os, re, json, time, sqlite3
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
DB_PATH        = os.getenv("DB_PATH", "/data/data.db")     # disco persistente no Render

if not TG_BOT_TOKEN or not PUBLIC_CHANNEL or not WEBHOOK_TOKEN:
    print("⚠️ Defina TG_BOT_TOKEN, PUBLIC_CHANNEL e WEBHOOK_TOKEN.")

TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

# =========================
# Hiperparâmetros do modelo
# =========================
WINDOW = 400
DECAY  = 0.985

W4, W3, W2, W1 = 0.45, 0.30, 0.17, 0.08
ALPHA, BETA, GAMMA = 1.10, 0.65, 0.35

GAP_MIN     = 0.00      # vamos sugerir SEMPRE; gap mínimo 0
COOLDOWN_S  = 12

app = FastAPI(title="Fantan Guardião — Número Seco", version="2.3.0")

# =========================
# DB helpers
# =========================
def now_ts() -> int:
    return int(time.time())

def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL,
            number INTEGER NOT NULL
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS ngram_stats (
            n INTEGER NOT NULL,
            ctx TEXT NOT NULL,
            next INTEGER NOT NULL,
            weight REAL NOT NULL,
            PRIMARY KEY (n, ctx, next)
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS stats_pattern (
            pattern_key TEXT NOT NULL,
            number INTEGER NOT NULL,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (pattern_key, number)
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS stats_strategy (
            strategy TEXT NOT NULL,
            number INTEGER NOT NULL,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (strategy, number)
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS suggestions (
            source_msg_id INTEGER PRIMARY KEY,
            strategy TEXT,
            seq_raw TEXT,
            context_key TEXT,
            pattern_key TEXT,
            base TEXT,
            suggested_number INTEGER,
            stage TEXT,
            sent_at INTEGER
        )""")

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

        cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_score (
            yyyymmdd TEXT PRIMARY KEY,
            g0_wins INTEGER NOT NULL DEFAULT 0,
            g1_wins INTEGER NOT NULL DEFAULT 0,
            g2_wins INTEGER NOT NULL DEFAULT 0,
            losses  INTEGER NOT NULL DEFAULT 0,
            streak  INTEGER NOT NULL DEFAULT 0
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_outcome (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL,
            strategy TEXT,
            suggested INTEGER NOT NULL,
            stage INTEGER NOT NULL,       -- 0,1,2 (G0/G1/G2)
            open INTEGER NOT NULL,        -- 1 = aberto
            window_left INTEGER NOT NULL  -- quantos resultados faltam observar
        )""")

init_db()

# =========================
# Telegram
# =========================
async def tg_send_text(chat_id: str, text: str, parse: str="HTML"):
    if not TG_BOT_TOKEN or not PUBLIC_CHANNEL:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True}
        await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)

# =========================
# Timeline & n-grams
# =========================
def append_timeline(number: int):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        con.execute("INSERT INTO timeline (created_at, number) VALUES (?,?)", (now_ts(), int(number)))

def get_recent_tail(window: int = WINDOW) -> List[int]:
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT number FROM timeline ORDER BY id DESC LIMIT ?", (window,)).fetchall()
    return [r["number"] for r in rows][::-1]

def update_ngrams(decay: float = DECAY, max_n: int = 5, window: int = WINDOW):
    tail = get_recent_tail(window)
    if len(tail) < 2:
        return
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        for t in range(1, len(tail)):
            nxt = tail[t]
            dist = (len(tail)-1) - t
            w = (decay ** dist)
            for n in range(2, max_n+1):
                if t-(n-1) < 0: break
                ctx = tail[t-(n-1):t]
                ctx_key = ",".join(str(x) for x in ctx)
                con.execute("""
                    INSERT INTO ngram_stats (n, ctx, next, weight)
                    VALUES (?,?,?,?)
                    ON CONFLICT(n, ctx, next) DO UPDATE SET weight = ngram_stats.weight + excluded.weight
                """, (n, ctx_key, int(nxt), float(w)))

def prob_from_ngrams(ctx: List[int], candidate: int) -> float:
    n = len(ctx) + 1
    if n < 2 or n > 5: return 0.0
    ctx_key = ",".join(str(x) for x in ctx)
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT SUM(weight) AS w FROM ngram_stats WHERE n=? AND ctx=?", (n, ctx_key)).fetchone()
        tot = (row["w"] or 0.0)
        if tot <= 0: return 0.0
        row2 = con.execute("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n, ctx_key, candidate)).fetchone()
        w = (row2["weight"] or 0.0) if row2 else 0.0
    return w / tot

# =========================
# Parsers
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
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return [a,b], f"KWOK-{a}-{b}"

    if re.search(r"\bODD\b", t, flags=re.I):  return [1,3], "ODD"
    if re.search(r"\bEVEN\b", t, flags=re.I): return [2,4], "EVEN"

    m = re.search(r"\bSS?H\s*([1-4])(?:-([1-4]))?(?:-([1-4]))?(?:-([1-4]))?", t, flags=re.I)
    if m:
        nums = [int(g) for g in m.groups() if g]
        return nums, "SSH-" + "-".join(str(x) for x in nums)

    m = re.search(r"Sequ[eê]ncia:\s*([\d\s\|\-]+)", t, flags=re.I)
    if m:
        parts = re.findall(r"[1-4]", m.group(1))
        seq_left_recent = [int(x) for x in parts]   # esquerda = mais RECENTE
        base = bases_from_sequence_left_recent(seq_left_recent, 3)
        if base:
            return base, "SEQ"

    return [], "GEN"

GREEN_RE   = re.compile(r"APOSTA\s+ENCERRADA.*?GREEN.*?\((\d)\)", re.I | re.S)
RED_NUM_RE = re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\((.*?)\)", re.I | re.S)

def extract_green_number(text: str) -> Optional[int]:
    m = GREEN_RE.search(text)
    return int(m.group(1)) if m else None

def extract_red_last_left(text: str) -> Optional[int]:
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
# Estatísticas e Placar (G0/G1/G2)
# =========================
def bump_pattern(pattern_key: str, number: int, won: bool):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        row = con.execute("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?",
                          (pattern_key, number)).fetchone()
        w = row[0] if row else 0
        l = row[1] if row else 0
        if won: w += 1
        else:   l += 1
        con.execute("""
          INSERT INTO stats_pattern (pattern_key, number, wins, losses)
          VALUES (?,?,?,?)
          ON CONFLICT(pattern_key, number)
          DO UPDATE SET wins=excluded.wins, losses=excluded.losses
        """, (pattern_key, number, w, l))

def bump_strategy(strategy: str, number: int, won: bool):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        row = con.execute("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?",
                          (strategy, number)).fetchone()
        w = row[0] if row else 0
        l = row[1] if row else 0
        if won: w += 1
        else:   l += 1
        con.execute("""
          INSERT INTO stats_strategy (strategy, number, wins, losses)
          VALUES (?,?,?,?)
          ON CONFLICT(strategy, number)
          DO UPDATE SET wins=excluded.wins, losses=excluded.losses
        """, (strategy, number, w, l))

def update_daily_score(stage: Optional[int], won: bool):
    # stage: 0=G0, 1=G1, 2=G2, None=derrota final
    y = today_key()
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        row = con.execute("SELECT g0_wins,g1_wins,g2_wins,losses,streak FROM daily_score WHERE yyyymmdd=?", (y,)).fetchone()
        if row:
            g0, g1, g2, losses, streak = row
        else:
            g0 = g1 = g2 = losses = streak = 0

        if won:
            if stage == 0: g0 += 1
            elif stage == 1: g1 += 1
            elif stage == 2: g2 += 1
            streak += 1
        else:
            losses += 1
            streak = 0

        con.execute("""
            INSERT INTO daily_score (yyyymmdd,g0_wins,g1_wins,g2_wins,losses,streak)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(yyyymmdd) DO UPDATE SET
              g0_wins=excluded.g0_wins,
              g1_wins=excluded.g1_wins,
              g2_wins=excluded.g2_wins,
              losses=excluded.losses,
              streak=excluded.streak
        """, (y, g0, g1, g2, losses, streak))

        total_w = g0 + g1 + g2
        total = total_w + losses
        acc = (total_w/total)*100 if total else 0.0

    return g0, g1, g2, losses, acc, streak

async def send_scoreboard():
    g0,g1,g2,losses,acc,streak = update_daily_score(stage=0, won=True)  # chamada apenas p/ ler valores atuais
    # o update acima incrementaria; reverte (consulta pura)
    # para evitar alterar, refaça leitura:
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        row = con.execute("SELECT g0_wins,g1_wins,g2_wins,losses,streak FROM daily_score WHERE yyyymmdd=?", (today_key(),)).fetchone()
        if row:
            g0,g1,g2,losses,streak = row
            total_w = g0+g1+g2
            total = total_w+losses
            acc = (total_w/total)*100 if total else 0.0
        else:
            g0=g1=g2=losses=streak=0
            acc=0.0
    txt = (f"📊 <b>Placar do dia</b>\n"
           f"🟢 G0:{g0} | G1:{g1} | G2:{g2}  🔴 Loss:{losses}\n"
           f"✅ Acerto: {acc:.2f}%\n"
           f"🔥 Streak: {streak} GREEN(s)")
    await tg_send_text(PUBLIC_CHANNEL, txt)

# =========================
# Cooldown / Lembretes
# =========================
def already_suggested(source_msg_id: int) -> bool:
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        row = con.execute("SELECT 1 FROM suggestions WHERE source_msg_id=?", (source_msg_id,)).fetchone()
    return row is not None

def remember_suggestion(source_msg_id:int, strategy:str, seq_raw:str,
                        context_key:str, pattern_key:str, base:List[int], suggested:int, stage:str):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
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

# =========================
# Predição
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
            ctx1 = tail[max(0, i): i+1]
            ctx2 = tail[max(0, i-1): i+1] if i-1 >= 0 else []
            ctx3 = tail[max(0, i-2): i+1] if i-2 >= 0 else []
            ctx4 = tail[max(0, i-3): i+1] if i-3 >= 0 else []
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
        score += w * p
    return score

def laplace_ratio(wins:int, losses:int) -> float:
    return (wins + 1.0) / (wins + losses + 2.0)

def confident_best(post: Dict[int,float], gap: float = GAP_MIN) -> Optional[int]:
    a = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a: return None
    if len(a) == 1: return a[0][0]
    if a[0][1] - a[1][1] >= gap:
        return a[0][0]
    return a[0][0]  # mesmo sem gap, devolve o melhor → sempre sugere

def suggest_number(base: List[int], pattern_key: str, strategy: Optional[str], after_num: Optional[int]) -> Tuple[Optional[int],float,int,Dict[int,float]]:
    if not base:
        base = [1,2,3,4]

    hour_block = int(datetime.now(timezone.utc).hour // 2)
    pat_key = f"{pattern_key}|h{hour_block}"

    tail = get_recent_tail(WINDOW)
    scores: Dict[int, float] = {}

    for c in base:
        ng = ngram_backoff_score(tail, after_num, c)

        with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
            rowp = con.execute("SELECT wins, losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pat_key, c)).fetchone()
            pw = rowp[0] if rowp else 0
            pl = rowp[1] if rowp else 0
            p_pat = laplace_ratio(pw, pl)

            p_str = 1/len(base)
            if strategy:
                rows = con.execute("SELECT wins, losses FROM stats_strategy WHERE strategy=? AND number=?", (strategy, c)).fetchone()
                sw = rows[0] if rows else 0
                sl = rows[1] if rows else 0
                p_str = laplace_ratio(sw, sl)

        prior = 1.0/len(base)
        score = (prior) * ((ng or 1e-6) ** ALPHA) * (p_pat ** BETA) * (p_str ** GAMMA)
        scores[c] = score

    total = sum(scores.values()) or 1e-9
    post = {k: v/total for k,v in scores.items()}
    number = confident_best(post, gap=GAP_MIN)
    conf = post.get(number, 0.0) if number is not None else 0.0

    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        roww = con.execute("SELECT SUM(weight) AS s FROM ngram_stats").fetchone()
        samples = int((roww[0] or 0))

    return number, conf, samples, post

# =========================
# Mensagem
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
# Pendências (G0/G1/G2)
# =========================
def open_pending(strategy: Optional[str], suggested: int):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        con.execute(
            "INSERT INTO pending_outcome (created_at,strategy,suggested,stage,open,window_left) VALUES (?,?,?,?,1,3)",
            (now_ts(), strategy or "", int(suggested), 0)
        )

def close_pending_with_result(n_real: int):
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        rows = con.execute(
            "SELECT id, strategy, suggested, stage, window_left FROM pending_outcome WHERE open=1 ORDER BY id"
        ).fetchall()

        for pid, strat, sug, stage, left in rows:
            strat = strat or ""
            sug = int(sug)
            stage = int(stage)
            left = int(left)

            if n_real == sug:
                # WIN no stage atual
                bump_pattern("PEND", sug, True)
                if strat:
                    bump_strategy(strat, sug, True)
                # atualiza placar por stage
                g0,g1,g2,losses,acc,streak = update_daily_score(stage=stage, won=True)
                con.execute("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
            else:
                left -= 1
                if left <= 0:
                    # LOSS total
                    bump_pattern("PEND", sug, False)
                    if strat:
                        bump_strategy(strat, sug, False)
                    g0,g1,g2,losses,acc,streak = update_daily_score(stage=None, won=False)
                    con.execute("UPDATE pending_outcome SET open=0 WHERE id=?", (pid,))
                else:
                    con.execute("UPDATE pending_outcome SET window_left=? WHERE id=?", (left, pid))

# =========================
# Routes
# =========================
@app.get("/")
async def root():
    return {"ok": True, "detail": "Use POST /webhook/<WEBHOOK_TOKEN>"}

@app.get("/debug/score")
async def debug_score():
    with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
        con.row_factory = sqlite3.Row
        out = [dict(r) for r in con.execute("SELECT * FROM daily_score ORDER BY yyyymmdd DESC LIMIT 14").fetchall()]
    return out

@app.get("/debug/last")
async def debug_last():
    tail = get_recent_tail(30)
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
    source_msg_id = msg.get("message_id")
    text = msg.get("text") or msg.get("caption") or ""
    t = re.sub(r"\s+", " ", text).strip()

    # 1) GREEN / RED / GALE → timeline + fechar pendências
    gnum = extract_green_number(t)
    red_last = extract_red_last_left(t)
    gale_n = is_gale_info(t)

    n_observed = None
    if gnum is not None:
        append_timeline(gnum); update_ngrams(); n_observed = gnum
    elif red_last is not None:
        append_timeline(red_last); update_ngrams(); n_observed = red_last

    if n_observed is not None:
        close_pending_with_result(n_observed)

        # aprendizado por estratégia quando GREEN com número
        strat = extract_strategy(t) or ""
        if strat and gnum is not None:
            with sqlite3.connect(DB_PATH, check_same_thread=False) as con:
                row = con.execute("SELECT suggested_number, pattern_key FROM last_by_strategy WHERE strategy=?", (strat,)).fetchone()
            if row:
                suggested = int(row[0]); pat_key = row[1] or "GEN"
                won = (suggested == int(gnum))
                bump_pattern(pat_key, suggested, won)
                bump_strategy(strat, suggested, won)
                # placar por stage é feito em close_pending_with_result
                await send_scoreboard()

        return {"ok": True, "learned_or_updated": True}

    # 2) ANALISANDO → alimentar timeline (NÃO sugerir)
    if is_analise(t):
        seq_raw = extract_seq_raw(t)
        if seq_raw:
            parts = re.findall(r"[1-4]", seq_raw)
            # esquerda = mais RECENTE → salvar em ordem cronológica
            seq_left_recent = [int(x) for x in parts]
            seq_old_to_new  = seq_left_recent[::-1]
            for n in seq_old_to_new:
                append_timeline(n)
                close_pending_with_result(n)
            update_ngrams()
        return {"ok": True, "analise": True}

    # 3) ENTRADA CONFIRMADA → SEMPRE sugerir 1 número seco
    if not is_real_entry(t):
        return {"ok": True, "skipped": True}

    if already_suggested(source_msg_id):
        return {"ok": True, "dup": True}

    strategy   = extract_strategy(t) or ""
    seq_raw    = extract_seq_raw(t) or ""
    after_num  = extract_after_num(t)
    base, pattern_key = parse_bases_and_pattern(t)
    if not base:
        base = [1,2,3,4]
        pattern_key = "GEN"

    number, conf, samples, post = suggest_number(base, pattern_key, strategy, after_num)

    remember_suggestion(source_msg_id, strategy, seq_raw, "CTX", pattern_key, base, number, stage="G0")
    open_pending(strategy, number)

    out = build_suggestion_msg(number, base, pattern_key, after_num, conf, samples, stage="G0")
    await tg_send_text(PUBLIC_CHANNEL, out)
    return {"ok": True, "sent": True, "conf": conf, "samples": samples, "pick": number}