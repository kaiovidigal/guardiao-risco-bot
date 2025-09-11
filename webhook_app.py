# -*- coding: utf-8 -*-
# Fan Tan — Guardião (G0 + recuperação oculta)
# Compacto: mesma lógica/performance, menos verbosidade.

import os, re, json, time, sqlite3, asyncio, shutil
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone, timedelta
from collections import Counter

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

# ========= ENV / CONFIG =========
DB_PATH       = os.getenv("DB_PATH", "/data/data.db").strip() or "/data/data.db"
TG_BOT_TOKEN  = os.getenv("TG_BOT_TOKEN", "").strip()
PUBLIC_CHANNEL= os.getenv("PUBLIC_CHANNEL", "").strip()
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
FLUSH_KEY     = os.getenv("FLUSH_KEY", "meusegredo123").strip()
TELEGRAM_API  = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
REPL_ENABLED  = True
REPL_CHANNEL  = "-1003052132833"

MAX_STAGE     = 3
FAST_RESULT   = True
SCORE_G0_ONLY = True

# Hiperparâmetros
WINDOW=400; DECAY=0.985
W4,W3,W2,W1 = 0.38,0.30,0.20,0.12
ALPHA,BETA,GAMMA = 1.05,0.70,0.40
GAP_MIN=0.08
MIN_CONF_G0=0.55; MIN_GAP_G0=0.04; MIN_SAMPLES=1000

# Inteligência em disco
INTEL_DIR=os.getenv("INTEL_DIR","/var/data/ai_intel").rstrip("/")
INTEL_MAX_BYTES=int(os.getenv("INTEL_MAX_BYTES","1000000000"))
INTEL_SIGNAL_INTERVAL=float(os.getenv("INTEL_SIGNAL_INTERVAL","20"))
INTEL_ANALYZE_INTERVAL=float(os.getenv("INTEL_ANALYZE_INTERVAL","2"))

SELF_LABEL_IA=os.getenv("SELF_LABEL_IA","Tiro seco por IA")

# IA2 thresholds base (fases sobrepõem)
IA2_TIER_STRICT=0.62
IA2_GAP_SAFETY=0.08
IA2_DELTA_GAP=0.03
IA2_MAX_PER_HOUR=10
IA2_COOLDOWN_AFTER_LOSS=20
IA2_MIN_SECONDS_BETWEEN_FIRE=15

# Fases adaptativas (amostra + acc 7d) + dwell
_S_A_MAX=20_000; _S_B_MAX=100_000
_ACC_TO_B=0.55; _ACC_TO_C=0.58
_ACC_FALL_B=0.52; _ACC_FALL_C=0.55
_PHASE_DWELL=60*60
_phase_name="A"; _phase_since_ts=0

# Estado IA2
_ia2_blocked_until_ts=0
_ia2_sent_this_hour=0
_ia2_hour_bucket=None
_ia2_last_fire_ts=0

# Razão (debug)
_ia2_last_reason="—"; _ia2_last_reason_ts=0

# ========= DB / Migração =========
def _ensure_db_dir(): os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
def _migrate_old_db_if_needed():
    if os.path.exists(DB_PATH): return
    for src in ["/var/data/data.db","/opt/render/project/src/data.db",
                "/opt/render/project/src/data/data.db","/data/data.db"]:
        if os.path.exists(src):
            try: _ensure_db_dir(); shutil.copy2(src, DB_PATH); print(f"[DB] Migrado {src} -> {DB_PATH}"); return
            except Exception as e: print(f"[DB] Erro migrando {src}: {e}")
_ensure_db_dir(); _migrate_old_db_if_needed()

def _connect()->sqlite3.Connection:
    _ensure_db_dir()
    con=sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory=sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con

def exec_write(sql:str, params:tuple=(), retries:int=8, wait:float=0.25):
    for _ in range(retries):
        try:
            con=_connect(); con.execute(sql, params); con.commit(); con.close(); return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower(): time.sleep(wait); continue
            raise
    raise sqlite3.OperationalError("Banco bloqueado.")

def query_all(sql:str, params:tuple=()) -> list[sqlite3.Row]:
    con=_connect(); rows=con.execute(sql, params).fetchall(); con.close(); return rows
def query_one(sql:str, params:tuple=()) -> Optional[sqlite3.Row]:
    con=_connect(); row=con.execute(sql, params).fetchone(); con.close(); return row

def init_db():
    con=_connect(); c=con.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, number INTEGER NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ngram_stats (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, next INTEGER NOT NULL, weight REAL NOT NULL,
        PRIMARY KEY (n, ctx, next))""")
    c.execute("""CREATE TABLE IF NOT EXISTS stats_pattern (
        pattern_key TEXT NOT NULL, number INTEGER NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (pattern_key, number))""")
    c.execute("""CREATE TABLE IF NOT EXISTS stats_strategy (
        strategy TEXT NOT NULL, number INTEGER NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (strategy, number))""")
    c.execute("""CREATE TABLE IF NOT EXISTS suggestions (
        source_msg_id INTEGER PRIMARY KEY, strategy TEXT, seq_raw TEXT, context_key TEXT, pattern_key TEXT,
        base TEXT, suggested_number INTEGER, stage TEXT, sent_at INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS last_by_strategy (
        strategy TEXT PRIMARY KEY, source_msg_id INTEGER, suggested_number INTEGER,
        context_key TEXT, pattern_key TEXT, stage TEXT, created_at INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS daily_score (
        yyyymmdd TEXT PRIMARY KEY, g0 INTEGER NOT NULL DEFAULT 0, g1 INTEGER NOT NULL DEFAULT 0,
        g2 INTEGER NOT NULL DEFAULT 0, loss INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS pending_outcome (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
        strategy TEXT, suggested INTEGER NOT NULL, stage INTEGER NOT NULL,
        open INTEGER NOT NULL, window_left INTEGER NOT NULL,
        seen_numbers TEXT DEFAULT '', announced INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'CHAN')""")
    c.execute("""CREATE TABLE IF NOT EXISTS daily_score_ia (
        yyyymmdd TEXT PRIMARY KEY, g0 INTEGER NOT NULL DEFAULT 0,
        loss INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS recov_g1_stats (
        number INTEGER PRIMARY KEY, wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0)""")
    con.commit(); con.close()
init_db()

# ========= Utils =========
def now_ts()->int: return int(time.time())
def utc_iso()->str: return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
def today_key_local()->str: return datetime.now(timezone.utc).strftime("%Y%m%d")
async def tg_send_text(chat_id:str, text:str, parse:str="HTML"):
    if not TG_BOT_TOKEN or not chat_id: return
    async with httpx.AsyncClient(timeout=15) as cl:
        await cl.post(f"{TELEGRAM_API}/sendMessage",
                      json={"chat_id":chat_id,"text":text,"parse_mode":parse,"disable_web_page_preview":True})
async def tg_broadcast(text:str, parse:str="HTML"):
    if REPL_ENABLED and REPL_CHANNEL: await tg_send_text(REPL_CHANNEL, text, parse)

def _get_scalar(sql:str, params:tuple=(), default=0):
    r=query_one(sql, params); 
    if not r: return default
    try: return r[0] if r[0] is not None else default
    except: 
        k=r.keys(); return r[k[0]] if k and r[k[0]] is not None else default

def _fmt_bytes(n:int)->str:
    try: n=float(n)
    except: return "—"
    for u in ["B","KB","MB","GB","TB","PB"]:
        if n<1024: return f"{n:.1f} {u}"
        n/=1024
    return f"{n:.1f} EB"
def _dir_size_bytes(path:str)->int:
    tot=0
    try:
        for root,_d,files in os.walk(path):
            for f in files:
                try: tot+=os.path.getsize(os.path.join(root,f))
                except: pass
    except: return 0
    return tot

def _mark_reason(r:str):
    global _ia2_last_reason,_ia2_last_reason_ts
    _ia2_last_reason=r; _ia2_last_reason_ts=now_ts()

# ========= Mensagens =========
async def send_green_imediato(n:int, stage_txt:str="G0"):
    await tg_broadcast(f"✅ <b>GREEN</b> em <b>{stage_txt}</b> — Número: <b>{n}</b>")
async def send_loss_imediato(n:int, stage_txt:str="G0"):
    await tg_broadcast(f"❌ <b>LOSS</b> — Número: <b>{n}</b> (em {stage_txt})")

async def ia2_send_signal(best:int, conf:float, tail_len:int, mode:str):
    await tg_broadcast(
        f"🤖 <b>{SELF_LABEL_IA} [{mode}]</b>\n"
        f"🎯 Número seco (G0): <b>{best}</b>\n"
        f"📈 Conf: <b>{conf*100:.2f}%</b> | Amostra≈<b>{tail_len}</b>"
    )

# ========= Timeline / n-grams =========
def append_timeline(n:int): exec_write("INSERT INTO timeline (created_at,number) VALUES (?,?)",(now_ts(),int(n)))
def get_recent_tail(window:int=WINDOW)->List[int]:
    rows=query_all("SELECT number FROM timeline ORDER BY id DESC LIMIT ?",(window,))
    return [r["number"] for r in rows][::-1]
def update_ngrams(decay:float=DECAY, max_n:int=5, window:int=WINDOW):
    tail=get_recent_tail(window)
    if len(tail)<2: return
    for t in range(1,len(tail)):
        nxt=tail[t]; dist=(len(tail)-1)-t; w=(decay**dist)
        for n in range(2,max_n+1):
            if t-(n-1)<0: break
            ctx=tail[t-(n-1):t]; ctx_key=",".join(str(x) for x in ctx)
            exec_write("""INSERT INTO ngram_stats (n,ctx,next,weight) VALUES (?,?,?,?)
                          ON CONFLICT(n,ctx,next) DO UPDATE SET weight=weight+excluded.weight""",
                       (n,ctx_key,int(nxt),float(w)))
def prob_from_ngrams(ctx:List[int], cand:int)->float:
    n=len(ctx)+1
    if n<2 or n>5: return 0.0
    ctx_key=",".join(str(x) for x in ctx)
    tot=(query_one("SELECT SUM(weight) FROM ngram_stats WHERE n=? AND ctx=?",(n,ctx_key)) or [0])[0] or 0.0
    if tot<=0: return 0.0
    w=(query_one("SELECT weight FROM ngram_stats WHERE n=? AND ctx=? AND next=?", (n,ctx_key,cand)) or [0])[0] or 0.0
    return w/tot

# ========= Parsers =========
MUST_HAVE=(r"ENTRADA\s+CONFIRMADA", r"Mesa:\s*Fantan\s*-\s*Evolution")
MUST_NOT =(r"\bANALISANDO\b", r"\bPlacar do dia\b", r"\bAPOSTA ENCERRADA\b")
def is_real_entry(text:str)->bool:
    t=re.sub(r"\s+"," ",text).strip()
    if any(re.search(b,t,flags=re.I) for b in MUST_NOT): return False
    if any(not re.search(g,t,flags=re.I) for g in MUST_HAVE): return False
    return any(re.search(p,t,flags=re.I) for p in [
        r"Sequ[eê]ncia:\s*[\d\s\|\-]+", r"\bKWOK\s*[1-4]\s*-\s*[1-4]",
        r"\bSS?H\s*[1-4](?:-[1-4]){0,3}", r"\bODD\b|\bEVEN\b", r"Entrar\s+ap[oó]s\s+o\s+[1-4]"])
def extract_strategy(t:str)->Optional[str]:
    m=re.search(r"Estrat[eé]gia:\s*(\d+)",t,flags=re.I); return m.group(1) if m else None
def extract_seq_raw(t:str)->Optional[str]:
    m=re.search(r"Sequ[eê]ncia:\s*([^\n\r]+)",t,flags=re.I); return m.group(1).strip() if m else None
def extract_after_num(t:str)->Optional[int]:
    m=re.search(r"Entrar\s+ap[oó]s\s+o\s+([1-4])",t,flags=re.I); return int(m.group(1)) if m else None
def bases_from_sequence_left_recent(seq:List[int], k:int=3)->List[int]:
    seen,base=set(),[]
    for n in seq:
        if n not in seen: seen.add(n); base.append(n)
        if len(base)==k: break
    return base
def parse_bases_and_pattern(t:str)->Tuple[List[int],str]:
    x=re.sub(r"\s+"," ",t).strip()
    m=re.search(r"\bKWOK\s*([1-4])\s*-\s*([1-4])",x,re.I)
    if m: a,b=int(m.group(1)),int(m.group(2)); return [a,b], f"KWOK-{a}-{b}"
    if re.search(r"\bODD\b",x,re.I):  return [1,3],"ODD"
    if re.search(r"\bEVEN\b",x,re.I): return [2,4],"EVEN"
    m=re.search(r"\bSS?H\s*([1-4])(?:-([1-4]))?(?:-([1-4]))?(?:-([1-4]))?",x,re.I)
    if m:
        nums=[int(g) for g in m.groups() if g]; return nums, "SSH-"+"-".join(str(x) for x in nums)
    m=re.search(r"Sequ[eê]ncia:\s*([\d\s\|\-]+)",x,re.I)
    if m:
        parts=re.findall(r"[1-4]",m.group(1)); base=bases_from_sequence_left_recent([int(x) for x in parts],3)
        if base: return base,"SEQ"
    return [],"GEN"

GREEN_PATTERNS=[re.compile(r"APOSTA\s+ENCERRADA.*?\bGREEN\b.*?\((\d)\)",re.I|re.S),
                re.compile(r"\bGREEN\b.*?Número[:\s]*([1-4])",re.I|re.S),
                re.compile(r"\bGREEN\b.*?\(([1-4])\)",re.I|re.S)]
RED_PATTERNS  =[re.compile(r"APOSTA\s+ENCERRADA.*?\bRED\b.*?\((.*?)\)",re.I|re.S),
                re.compile(r"\bLOSS\b.*?Número[:\s]*([1-4])",re.I|re.S),
                re.compile(r"\bRED\b.*?\(([1-4])\)",re.I|re.S)]
def extract_green_number(t:str)->Optional[int]:
    s=re.sub(r"\s+"," ",t)
    for rx in GREEN_PATTERNS:
        m=rx.search(s)
        if m:
            nums=re.findall(r"[1-4]",m.group(1))
            if nums: return int(nums[0])
    return None
def extract_red_last_left(t:str)->Optional[int]:
    s=re.sub(r"\s+"," ",t)
    for rx in RED_PATTERNS:
        m=rx.search(s)
        if m:
            nums=re.findall(r"[1-4]",m.group(1))
            if nums: return int(nums[0])
    return None
def is_analise(t:str)->bool: return bool(re.search(r"\bANALISANDO\b",t,re.I))

# ========= Placar =========
def laplace_ratio(w:int,l:int)->float: return (w+1.0)/(w+l+2.0)
def bump_pattern(key:str, num:int, won:bool):
    r=query_one("SELECT wins,losses FROM stats_pattern WHERE pattern_key=? AND number=?",(key,num))
    w=(r["wins"] if r else 0)+(1 if won else 0); l=(r["losses"] if r else 0)+(0 if won else 1)
    exec_write("""INSERT INTO stats_pattern (pattern_key,number,wins,losses) VALUES (?,?,?,?)
                  ON CONFLICT(pattern_key,number) DO UPDATE SET wins=excluded.wins, losses=excluded.losses""",
               (key,num,w,l))
def bump_strategy(st:str, num:int, won:bool):
    r=query_one("SELECT wins,losses FROM stats_strategy WHERE strategy=? AND number=?",(st,num))
    w=(r["wins"] if r else 0)+(1 if won else 0); l=(r["losses"] if r else 0)+(0 if won else 1)
    exec_write("""INSERT INTO stats_strategy (strategy,number,wins,losses) VALUES (?,?,?,?)
                  ON CONFLICT(strategy,number) DO UPDATE SET wins=excluded.wins, losses=excluded.losses""",
               (st,num,w,l))

def update_daily_score(stage:Optional[int], won:bool):
    y=today_key_local()
    r=query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0,g1,g2,loss,streak=(r["g0"] if r else 0, r["g1"] if r else 0, r["g2"] if r else 0, r["loss"] if r else 0, r["streak"] if r else 0)
    if SCORE_G0_ONLY:
        if won and stage==0: g0+=1; streak+=1
        elif not won: loss+=1; streak=0
    else:
        if won: (g0,g1,g2)[stage]+=1; streak+=1
        else: loss+=1; streak=0
    exec_write("""INSERT INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,?,?,?,?,?)
                  ON CONFLICT(yyyymmdd) DO UPDATE SET g0=excluded.g0,g1=excluded.g1,g2=excluded.g2,loss=excluded.loss,streak=excluded.streak""",
               (y,g0,g1,g2,loss,streak))
def update_daily_score_ia(stage:Optional[int], won:bool):
    y=today_key_local()
    r=query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    g0,loss,streak=(r["g0"] if r else 0, r["loss"] if r else 0, r["streak"] if r else 0)
    if won and stage==0: g0+=1; streak+=1
    elif not won: loss+=1; streak=0
    exec_write("""INSERT INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,?,?,?)
                  ON CONFLICT(yyyymmdd) DO UPDATE SET g0=excluded.g0,loss=excluded.loss,streak=excluded.streak""",(y,g0,loss,streak))
def _convert_last_loss_to_green():
    y=today_key_local()
    r=query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    if not r: exec_write("""INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,0,0,0,0,0)""",(y,)); return
    g0,loss,streak=r["g0"] or 0, r["loss"] or 0, r["streak"] or 0
    if loss>0:
        loss-=1; g0+=1; streak+=1
        exec_write("""INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,?,?,?,?,?)""",
                   (y,g0,r["g1"] or 0,r["g2"] or 0,loss,streak))
def _convert_last_loss_to_green_ia():
    y=today_key_local()
    r=query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    if not r: exec_write("""INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,0,0,0)""",(y,)); return
    g0,loss,streak=r["g0"] or 0, r["loss"] or 0, r["streak"] or 0
    if loss>0:
        loss-=1; g0+=1; streak+=1
        exec_write("""INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,?,?,?)""",(y,g0,loss,streak))

# ========= Recov G1 =========
def bump_recov_g1(n:int, won:bool):
    r=query_one("SELECT wins,losses FROM recov_g1_stats WHERE number=?", (int(n),))
    w=(r["wins"] if r else 0)+(1 if won else 0); l=(r["losses"] if r else 0)+(0 if won else 1)
    exec_write("""INSERT INTO recov_g1_stats (number,wins,losses) VALUES (?,?,?)
                  ON CONFLICT(number) DO UPDATE SET wins=excluded.wins, losses=excluded.losses""",(int(n),w,l))
def recov_g1_rate(n:int)->Tuple[float,int]:
    r=query_one("SELECT wins,losses FROM recov_g1_stats WHERE number=?", (int(n),))
    if not r: return 0.0,0
    w,l=r["wins"] or 0, r["losses"] or 0
    t=w+l; return (w/t if t>0 else 0.0), t

# ========= IA métricas =========
def _ia_acc_days(days:int=7)->Tuple[int,int,float]:
    days=max(1,min(int(days),30))
    rows=query_all("SELECT g0,loss FROM daily_score_ia ORDER BY yyyymmdd DESC LIMIT ?",(days,))
    g=sum((r["g0"] or 0) for r in rows); l=sum((r["loss"] or 0) for r in rows)
    acc=(g/(g+l)) if (g+l)>0 else 0.0
    return int(g),int(l),float(acc)

# ========= Health / Relatório =========
def _daily_score_snapshot():
    y=today_key_local(); r=query_one("SELECT g0,g1,g2,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    if not r: return 0,0,0,0,0.0
    g0=r["g0"] or 0; loss=r["loss"] or 0; streak=r["streak"] or 0
    total=g0+loss; acc=(g0/total*100) if total else 0.0
    return g0,loss,streak,total,acc
def _daily_score_snapshot_ia():
    y=today_key_local(); r=query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    g0=r["g0"] if r else 0; loss=r["loss"] if r else 0; total=g0+loss; acc=(g0/total*100) if total else 0.0
    return g0,loss,total,acc
def _last_snapshot_info():
    p=os.path.join(INTEL_DIR,"snapshots","latest_top.json")
    if not os.path.exists(p): return "—"
    ts=os.path.getmtime(p)
    return datetime.utcfromtimestamp(ts).replace(tzinfo=timezone.utc).isoformat()

def _health_text()->str:
    timeline_cnt=_get_scalar("SELECT COUNT(*) FROM timeline")
    ngram_rows=_get_scalar("SELECT COUNT(*) FROM ngram_stats")
    ngram_samples=_get_scalar("SELECT SUM(weight) FROM ngram_stats")
    pat_rows=_get_scalar("SELECT COUNT(*) FROM stats_pattern")
    pat_events=_get_scalar("SELECT SUM(wins+losses) FROM stats_pattern")
    strat_rows=_get_scalar("SELECT COUNT(*) FROM stats_strategy")
    strat_events=_get_scalar("SELECT SUM(wins+losses) FROM stats_strategy")
    pend_open=_get_scalar("SELECT COUNT(*) FROM pending_outcome WHERE open=1")
    intel_size=_dir_size_bytes(INTEL_DIR)
    last_snap_ts=_last_snapshot_info()
    g0,loss,streak,total,acc=_daily_score_snapshot()
    ia_g0,ia_loss,ia_total,ia_acc=_daily_score_snapshot_ia()
    try:
        last_age=max(0, now_ts()-(_ia2_last_reason_ts or now_ts()))
        last_line=f"🤖 Motivo último NÃO-FIRE/FIRE: {_ia2_last_reason} (há {last_age}s)"
    except: last_line="🤖 Motivo último NÃO-FIRE/FIRE: —"
    try:
        eff=_eff(); _g7,_l7,acc7=_ia_acc_days(7)
        phase_line=(f"🧭 Fase: {eff['PHASE']} | Max/h: {eff['IA2_MAX_PER_HOUR']} | "
                    f"Conc.: {eff['MAX_CONCURRENT_PENDINGS']} | Conf≥{eff['IA2_TIER_STRICT']:.2f} "
                    f"(flex −{eff['IA2_DELTA_GAP']:.2f} c/gap≥{eff['IA2_GAP_SAFETY']:.2f}) | "
                    f"G0 conf/gap≥{eff['MIN_CONF_G0']:.2f}/{eff['MIN_GAP_G0']:.3f} | IA 7d: {(_g7+_l7)} • {acc7*100:.2f}%")
    except: phase_line="🧭 Fase: —"
    return ("🩺 <b>Saúde do Guardião</b>\n"
            f"⏱️ UTC: <code>{utc_iso()}</code>\n—\n"
            f"🗄️ timeline: <b>{timeline_cnt}</b>\n"
            f"📚 ngram_stats: <b>{ngram_rows}</b> | amostras≈<b>{int(ngram_samples or 0)}</b>\n"
            f"🧩 stats_pattern: chaves=<b>{pat_rows}</b> | eventos=<b>{int(pat_events or 0)}</b>\n"
            f"🧠 stats_strategy: chaves=<b>{strat_rows}</b> | eventos=<b>{int(strat_events or 0)}</b>\n"
            f"⏳ pendências abertas: <b>{pend_open}</b>\n"
            f"💾 INTEL dir: <b>{_fmt_bytes(intel_size)}</b> | último snapshot: <code>{last_snap_ts}</code>\n—\n"
            f"📊 Placar (hoje - G0 only): G0=<b>{g0}</b> | Loss=<b>{loss}</b> | Total=<b>{total}</b>\n"
            f"✅ Acerto: <b>{acc:.2f}%</b> | 🔥 Streak: <b>{streak}</b>\n—\n"
            f"🤖 <b>IA</b> G0=<b>{ia_g0}</b> | Loss=<b>{ia_loss}</b> | Total=<b>{ia_total}</b>\n"
            f"✅ <b>IA Acerto (dia):</b> <b>{ia_acc:.2f}%</b>\n"
            f"{last_line}\n{phase_line}\n")

def _fmt_pct(num:int, den:int)->float: return (num/den*100.0) if den else 0.0
def _read_daily_score_days(n:int=7): return query_all("""SELECT yyyymmdd,g0,g1,g2,loss,streak
                                                         FROM daily_score ORDER BY yyyymmdd DESC LIMIT ?""",(n,))
def _read_daily_score_ia_days(n:int=7): return query_all("""SELECT yyyymmdd,g0,loss,streak
                                                            FROM daily_score_ia ORDER BY yyyymmdd DESC LIMIT ?""",(n,))
def _mk_relatorio_text(days:int=7)->str:
    days=max(1,min(int(days),30)); y=today_key_local()
    r=query_one("SELECT g0,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,))
    g0=(r["g0"] if r else 0); loss=(r["loss"] if r else 0); acc_today=_fmt_pct(g0,g0+loss)
    ria=query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
    ia_g0=(ria["g0"] if ria else 0); ia_loss=(ria["loss"] if ria else 0); ia_acc_today=_fmt_pct(ia_g0, ia_g0+ia_loss)
    chan=_read_daily_score_days(days); ia=_read_daily_score_ia_days(days)
    sum_g0=sum((x["g0"] or 0) for x in chan); sum_loss=sum((x["loss"] or 0) for x in chan)
    acc_ndays=_fmt_pct(sum_g0, sum_g0+sum_loss)
    sum_ia_g0=sum((x["g0"] or 0) for x in ia); sum_ia_loss=sum((x["loss"] or 0) for x in ia)
    ia_acc_ndays=_fmt_pct(sum_ia_g0, sum_ia_g0+sum_ia_loss)
    return ("📈 <b>Relatório de Desempenho</b>\n"
            f"🗓️ Janela: <b>hoje</b> e últimos <b>{days}</b> dias\n—\n"
            f"📣 <b>Canal (hoje)</b>: G0=<b>{g0}</b> | Loss=<b>{loss}</b> | Acerto=<b>{acc_today:.2f}%</b>\n"
            f"🤖 <b>IA (hoje)</b>: G0=<b>{ia_g0}</b> | Loss=<b>{ia_loss}</b> | Acerto=<b>{ia_acc_today:.2f}%</b>\n—\n"
            f"📣 <b>Canal ({days}d)</b>: G0=<b>{sum_g0}</b> | Loss=<b>{sum_loss}</b> | Acerto=<b>{acc_ndays:.2f}%</b>\n"
            f"🤖 <b>IA ({days}d)</b>: G0=<b>{sum_ia_g0}</b> | Loss=<b>{sum_ia_loss}</b> | Acerto=<b>{ia_acc_ndays:.2f}%</b>\n")

# ========= Intel (stub) =========
class _IntelStub:
    def __init__(self, base_dir:str):
        self.base_dir=base_dir; os.makedirs(os.path.join(base_dir,"snapshots"), exist_ok=True)
        self._signal_active=False; self._last_num=None
    def start_signal(self, suggested:int, strategy:Optional[str]=None):
        self._signal_active=True; self._last_num=suggested
        try:
            p=os.path.join(self.base_dir,"snapshots","latest_top.json")
            with open(p,"w") as f: json.dump({"ts":now_ts(),"suggested":suggested,"strategy":strategy}, f)
        except Exception as e: print(f"[INTEL] snapshot error: {e}")
    def stop_signal(self): self._signal_active=False; self._last_num=None
INTEL=_IntelStub(INTEL_DIR)

# ========= Heurística extra: top-2 da cauda(40) =========
def tail_top2_boost(tail:List[int], k:int=40)->Dict[int,float]:
    boosts={1:1.00,2:1.00,3:1.00,4:1.00}
    if not tail: return boosts
    t=tail[-k:] if len(tail)>=k else tail[:]
    c=Counter(t); freq=c.most_common()
    if len(freq)>=1: boosts[freq[0][0]]=1.04
    if len(freq)>=2: boosts[freq[1][0]]=1.02
    return boosts

# ========= Predição =========
def ngram_backoff_score(tail:List[int], after:Optional[int], cand:int)->float:
    if not tail: return 0.0
    if after is not None:
        idxs=[i for i,v in enumerate(tail) if v==after]
        if not idxs:
            ctx4=tail[-4:] if len(tail)>=4 else []; ctx3=tail[-3:] if len(tail)>=3 else []
            ctx2=tail[-2:] if len(tail)>=2 else []; ctx1=tail[-1:] if len(tail)>=1 else []
        else:
            i=idxs[-1]
            ctx1=tail[max(0,i):i+1]
            ctx2=tail[max(0,i-1):i+1] if i-1>=0 else []
            ctx3=tail[max(0,i-2):i+1] if i-2>=0 else []
            ctx4=tail[max(0,i-3):i+1] if i-3>=0 else []
    else:
        ctx4=tail[-4:] if len(tail)>=4 else []
        ctx3=tail[-3:] if len(tail)>=3 else []
        ctx2=tail[-2:] if len(tail)>=2 else []
        ctx1=tail[-1:] if len(tail)>=1 else []
    parts=[]
    if len(ctx4)==4: parts.append((W4, prob_from_ngrams(ctx4[:-1], cand)))
    if len(ctx3)==3: parts.append((W3, prob_from_ngrams(ctx3[:-1], cand)))
    if len(ctx2)==2: parts.append((W2, prob_from_ngrams(ctx2[:-1], cand)))
    if len(ctx1)==1: parts.append((W1, prob_from_ngrams(ctx1[:-1], cand)))
    return sum(w*p for w,p in parts)

def confident_best(post:Dict[int,float], gap:float=GAP_MIN)->Optional[int]:
    a=sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    if not a: return None
    if len(a)==1: return a[0][0]
    return a[0][0] if (a[0][1]-a[1][1])>=gap else None

def suggest_number(base:List[int], pattern_key:str, strategy:Optional[str], after:Optional[int])->Tuple[Optional[int],float,int,Dict[int,float]]:
    if not base: base=[1,2,3,4]
    hour_block=int(datetime.now(timezone.utc).hour//2); pat_key=f"{pattern_key}|h{hour_block}"
    tail=get_recent_tail(WINDOW); scores:Dict[int,float]={}
    if after is not None:
        try: last_idx=max([i for i,v in enumerate(tail) if v==after])
        except ValueError: return None,0.0,len(tail),{k:1/len(base) for k in base}
        if (len(tail)-1-last_idx)>60: return None,0.0,len(tail),{k:1/len(base) for k in base}
    boost_tail=tail_top2_boost(tail, k=40)
    for c in base:
        ng=ngram_backoff_score(tail, after, c)
        r=query_one("SELECT wins,losses FROM stats_pattern WHERE pattern_key=? AND number=?", (pat_key,c))
        pw=r["wins"] if r else 0; pl=r["losses"] if r else 0; p_pat=laplace_ratio(pw,pl)
        p_str=1/len(base)
        if strategy:
            rs=query_one("SELECT wins,losses FROM stats_strategy WHERE strategy=? AND number=?",(strategy,c))
            sw=rs["wins"] if rs else 0; sl=rs["losses"] if rs else 0; p_str=laplace_ratio(sw,sl)
        boost_pat=1.05 if p_pat>=0.60 else (0.97 if p_pat<=0.40 else 1.00)
        boost_hist=boost_tail.get(c,1.00)
        prior=1.0/len(base)
        scores[c]=(prior)*((ng or 1e-6)**ALPHA)*(p_pat**BETA)*(p_str**GAMMA)*boost_pat*boost_hist
    tot=sum(scores.values()) or 1e-9
    post={k:v/tot for k,v in scores.items()}
    number=confident_best(post, gap=GAP_MIN)
    conf=post.get(number,0.0) if number is not None else 0.0
    samples=int((query_one("SELECT SUM(weight) AS s FROM ngram_stats")["s"] or 0) if query_one("SELECT SUM(weight) AS s FROM ngram_stats") else 0)
    last=query_one("SELECT suggested,announced FROM pending_outcome ORDER BY id DESC LIMIT 1")
    if last and number is not None and last["suggested"]==number and (last["announced"] or 0)==1:
        effx=_eff()
        if post.get(number,0.0)<(effx["MIN_CONF_G0"]+0.08): return None,0.0,samples,post
    top=sorted(post.items(), key=lambda kv: kv[1], reverse=True)[:2]
    if len(top)==2 and (top[0][1]-top[1][1])<0.015: return None,0.0,samples,post
    eff=_eff(); MIN_CONF_EFF=eff["MIN_CONF_G0"]; MIN_GAP_EFF=eff["MIN_GAP_G0"]
    gap=(top[0][1]-(top[1][1] if len(top)>1 else 0.0)) if top else 0.0
    if samples>=MIN_SAMPLES and ((number is None) or (post.get(number,0.0)<MIN_CONF_EFF) or (gap<MIN_GAP_EFF)):
        return None,0.0,samples,post
    return number, conf, samples, post

def build_suggestion_msg(number:int, base:List[int], pattern_key:str, after:Optional[int], conf:float, samples:int, stage:str="G0")->str:
    base_txt=", ".join(str(x) for x in base) if base else "—"; aft=f" após {after}" if after else ""
    return (f"🎯 <b>Número seco ({stage}):</b> <b>{number}</b>\n"
            f"🧩 <b>Padrão:</b> {pattern_key}{aft}\n"
            f"🧮 <b>Base:</b> [{base_txt}]\n"
            f"📊 Conf: {conf*100:.2f}% | Amostra≈{samples}")

# ========= IA2 loop =========
def _current_samples()->int:
    r=query_one("SELECT SUM(weight) AS s FROM ngram_stats"); return int((r["s"] or 0) if r else 0)
def _decide_phase(samples:int, acc7:float)->str:
    if samples<_S_A_MAX: return "A"
    if samples<_S_B_MAX: return "B" if acc7>=_ACC_TO_B else "A"
    if acc7>=_ACC_TO_C: return "C"
    if acc7>=_ACC_TO_B: return "B"
    return "A" if acc7<_ACC_FALL_B else "B"
def _adaptive_phase()->str:
    global _phase_name,_phase_since_ts
    s=_current_samples(); _g,_l,acc7=_ia_acc_days(7); target=_decide_phase(s,acc7); now=now_ts()
    if _phase_since_ts==0: _phase_name,target; _phase_since_ts=now; _phase_name=target; return _phase_name
    if target==_phase_name: return _phase_name
    if target>_phase_name:
        if now-_phase_since_ts<_PHASE_DWELL: return _phase_name
        _phase_name, _phase_since_ts = target, now; return _phase_name
    if _phase_name=="C" and acc7<_ACC_FALL_C: _phase_name,_phase_since_ts="B",now; return _phase_name
    if _phase_name=="B" and acc7<_ACC_FALL_B: _phase_name,_phase_since_ts="A",now; return _phase_name
    if now-_phase_since_ts<_PHASE_DWELL: return _phase_name
    _phase_name,_phase_since_ts=target,now; return _phase_name
def _eff()->dict:
    p=_adaptive_phase()
    if p=="A":
        return {"PHASE":"A","MAX_CONCURRENT_PENDINGS":2,"IA2_MAX_PER_HOUR":30,"IA2_MIN_SECONDS_BETWEEN_FIRE":7,
                "IA2_COOLDOWN_AFTER_LOSS":10,"IA2_TIER_STRICT":0.59,"IA2_DELTA_GAP":0.04,
                "IA2_GAP_SAFETY":IA2_GAP_SAFETY,"MIN_CONF_G0":0.52,"MIN_GAP_G0":0.035}
    if p=="B":
        return {"PHASE":"B","MAX_CONCURRENT_PENDINGS":2,"IA2_MAX_PER_HOUR":20,"IA2_MIN_SECONDS_BETWEEN_FIRE":10,
                "IA2_COOLDOWN_AFTER_LOSS":12,"IA2_TIER_STRICT":0.60,"IA2_DELTA_GAP":0.04,
                "IA2_GAP_SAFETY":IA2_GAP_SAFETY,"MIN_CONF_G0":0.53,"MIN_GAP_G0":0.035}
    return {"PHASE":"C","MAX_CONCURRENT_PENDINGS":1,"IA2_MAX_PER_HOUR":12,"IA2_MIN_SECONDS_BETWEEN_FIRE":12,
            "IA2_COOLDOWN_AFTER_LOSS":15,"IA2_TIER_STRICT":0.62,"IA2_DELTA_GAP":0.03,
            "IA2_GAP_SAFETY":IA2_GAP_SAFETY,"MIN_CONF_G0":0.55,"MIN_GAP_G0":0.040}

def _ia2_hour_key()->int: return int(datetime.now(timezone.utc).strftime("%Y%m%d%H"))
def _ia2_reset_hour():
    global _ia2_sent_this_hour,_ia2_hour_bucket
    hb=_ia2_hour_key()
    if _ia2_hour_bucket!=hb: _ia2_hour_bucket=hb; _ia2_sent_this_hour=0
def _ia2_antispam_ok()->bool:
    eff=_eff(); _ia2_reset_hour(); return _ia2_sent_this_hour<eff["IA2_MAX_PER_HOUR"]
def _ia2_mark_sent(): 
    global _ia2_sent_this_hour; _ia2_sent_this_hour+=1
def _ia2_blocked_now()->bool: return now_ts()<_ia2_blocked_until_ts
def _ia_set_post_loss_block():
    global _ia2_blocked_until_ts
    eff=_eff(); _ia2_blocked_until_ts=now_ts()+int(eff["IA2_COOLDOWN_AFTER_LOSS"])
def _has_open_pending()->bool:
    eff=_eff(); open_cnt=int(_get_scalar("SELECT COUNT(*) FROM pending_outcome WHERE open=1"))
    return open_cnt>=eff["MAX_CONCURRENT_PENDINGS"]
def _ia2_can_fire_now()->bool:
    eff=_eff()
    if _ia2_blocked_now(): return False
    if not _ia2_antispam_ok(): return False
    if now_ts()-_ia2_last_fire_ts<eff["IA2_MIN_SECONDS_BETWEEN_FIRE"]: return False
    return True
def _ia2_mark_fire_sent():
    global _ia2_last_fire_ts
    _ia2_mark_sent(); _ia2_last_fire_ts=now_ts()

def open_pending(strategy:Optional[str], suggested:int, source:str="CHAN"):
    exec_write("""INSERT INTO pending_outcome
                  (created_at,strategy,suggested,stage,open,window_left,seen_numbers,announced,source)
                  VALUES (?,?,?,?,1,?, '', 0, ?)""", (now_ts(), strategy or "", int(suggested), 0, MAX_STAGE, source))
    try: INTEL.start_signal(suggested=int(suggested), strategy=strategy)
    except Exception as e: print(f"[INTEL] start_signal error: {e}")

async def close_pending_with_result(n_observed:int, event_kind:str):
    rows=query_all("""SELECT id,created_at,strategy,suggested,stage,open,window_left,seen_numbers,announced,source
                      FROM pending_outcome WHERE open=1 ORDER BY id ASC""")
    if not rows: return {"ok":True,"no_open":True}
    for r in rows:
        pid=r["id"]; suggested=int(r["suggested"]); stage=int(r["stage"])
        left=int(r["window_left"]); src=(r["source"] or "CHAN").upper()
        seen=(r["seen_numbers"] or "").strip(); seen_new=(seen+("," if seen else "")+str(int(n_observed)))
        if int(n_observed)==suggested:
            exec_write("UPDATE pending_outcome SET open=0,window_left=0,seen_numbers=? WHERE id=?", (seen_new,pid))
            if stage==0:
                update_daily_score(0,True)
                if src=="IA": update_daily_score_ia(0,True)
                await send_green_imediato(suggested,"G0")
            else:
                _convert_last_loss_to_green()
                if src=="IA": _convert_last_loss_to_green_ia()
                bump_recov_g1(suggested,True)
            try: INTEL.stop_signal()
            except: pass
        else:
            if left>1:
                exec_write("""UPDATE pending_outcome SET stage=stage+1, window_left=window_left-1, seen_numbers=?
                              WHERE id=?""",(seen_new,pid))
                if stage==0:
                    update_daily_score(0,False)
                    if src=="IA": update_daily_score_ia(0,False)
                    await send_loss_imediato(suggested,"G0")
                    _ia_set_post_loss_block()
                    bump_recov_g1(suggested,False)
            else:
                exec_write("UPDATE pending_outcome SET open=0, window_left=0, seen_numbers=? WHERE id=?",(seen_new,pid))
    return {"ok":True}

def _relevance_ok(conf_raw:float, gap:float, tail_len:int)->bool:
    eff=_eff(); strict=eff["IA2_TIER_STRICT"]; delta=eff["IA2_DELTA_GAP"]; gap_s=eff["IA2_GAP_SAFETY"]
    return (conf_raw>=strict or (conf_raw>=strict-delta and gap>=gap_s)) and (tail_len>=MIN_SAMPLES)

async def ia2_process_once():
    base,pattern_key,strategy,after=[], "GEN", None, None
    tail=get_recent_tail(WINDOW)
    best,conf_raw,tail_len,post=suggest_number(base,pattern_key,strategy,after)
    if best is None:
        _mark_reason(f"amostra_insuficiente({tail_len}<{MIN_SAMPLES})" if tail_len<MIN_SAMPLES else "sem_numero_confiavel(conf/gap)")
        return
    gap=1.0
    if post and len(post)>=2:
        top=sorted(post.items(), key=lambda kv: kv[1], reverse=True)
        if len(top)>=2: gap=top[0][1]-top[1][1]
    if _has_open_pending(): _mark_reason("pendencia_aberta(aguardando G1/G2)"); return
    if _ia2_blocked_now(): _mark_reason("cooldown_pos_loss"); return
    if not _ia2_antispam_ok(): _mark_reason("limite_hora_atingido"); return
    eff=_eff()
    if now_ts()-_ia2_last_fire_ts<eff["IA2_MIN_SECONDS_BETWEEN_FIRE"]: _mark_reason("espacamento_minimo"); return
    if _relevance_ok(conf_raw,gap,tail_len) and _ia2_can_fire_now():
        open_pending(strategy, best, source="IA")
        await ia2_send_signal(best, conf_raw, tail_len, "FIRE")
        _ia2_mark_fire_sent()
        _mark_reason(f"FIRE(best={best}, conf={conf_raw:.3f}, gap={gap:.3f}, tail={tail_len})")
        return
    _mark_reason(f"reprovado_relevancia(conf={conf_raw:.3f}, gap={gap:.3f}, tail={tail_len})")

# ========= API =========
app=FastAPI(title="Fantan Guardião — FIRE-only (G0 + Recuperação oculta)", version="3.10.0")

class Update(BaseModel):
    update_id:int
    channel_post:Optional[dict]=None
    message:Optional[dict]=None
    edited_channel_post:Optional[dict]=None
    edited_message:Optional[dict]=None

@app.get("/")
async def root(): return {"ok":True,"detail":"Use POST /webhook/<WEBHOOK_TOKEN>"}

@app.on_event("startup")
async def _boot_tasks():
    async def _health_loop():
        while True:
            try:
                await tg_broadcast(_health_text())
                try: await tg_broadcast(_mk_relatorio_text(days=7))
                except Exception as e: print(f"[RELATORIO] erro: {e}")
            except Exception as e: print(f"[HEALTH] erro: {e}")
            await asyncio.sleep(30*60)
    async def _daily_reset_loop():
        while True:
            try:
                now=datetime.now(timezone.utc)
                tomorrow=(now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
                await asyncio.sleep(max(1.0,(tomorrow-now).total_seconds()))
                y=today_key_local()
                exec_write("""INSERT OR REPLACE INTO daily_score (yyyymmdd,g0,g1,g2,loss,streak) VALUES (?,0,0,0,0,0)""",(y,))
                exec_write("""INSERT OR REPLACE INTO daily_score_ia (yyyymmdd,g0,loss,streak) VALUES (?,0,0,0)""",(y,))
                await tg_broadcast("🕛 <b>Reset diário executado (00:00 UTC)</b>\n📊 Placar geral e IA zerados para o novo dia.")
            except Exception as e: print(f"[RESET] erro: {e}"); await asyncio.sleep(60)
    async def _ia2_loop():
        while True:
            try: await ia2_process_once()
            except Exception as e: print(f"[IA2] analyzer error: {e}")
            await asyncio.sleep(max(0.2, INTEL_ANALYZE_INTERVAL))
    try: asyncio.create_task(_health_loop())
    except Exception as e: print(f"[HEALTH] startup error: {e}")
    try: asyncio.create_task(_daily_reset_loop())
    except Exception as e: print(f"[RESET] startup error: {e}")
    try: asyncio.create_task(_ia2_loop())
    except Exception as e: print(f"[IA2] startup error: {e}")

@app.post("/webhook/{token}")
async def webhook(token:str, request:Request):
    if token!=WEBHOOK_TOKEN: raise HTTPException(status_code=403, detail="Forbidden")
    data=await request.json(); upd=Update(**data)
    msg=upd.channel_post or upd.message or upd.edited_channel_post or upd.edited_message
    if not msg: return {"ok":True}
    text=(msg.get("text") or msg.get("caption") or "").strip(); t=re.sub(r"\s+"," ",text)

    gnum=extract_green_number(t); redn=extract_red_last_left(t)
    if gnum is not None or redn is not None:
        n=gnum if gnum is not None else redn; kind="GREEN" if gnum is not None else "RED"
        append_timeline(n); update_ngrams(); await close_pending_with_result(n, kind)
        strat=extract_strategy(t) or ""
        row=query_one("SELECT suggested_number,pattern_key FROM last_by_strategy WHERE strategy=?", (strat,))
        if row and gnum is not None:
            suggested=int(row["suggested_number"]); pat_key=row["pattern_key"] or "GEN"; won=(suggested==int(gnum))
            bump_pattern(pat_key, suggested, won); 
            if strat: bump_strategy(strat, suggested, won)
        await ia2_process_once()
        return {"ok":True,"observed":n,"kind":kind}

    if is_analise(t):
        seq_raw=extract_seq_raw(t)
        if seq_raw:
            parts=re.findall(r"[1-4]",seq_raw); seq=[int(x) for x in parts][::-1]
            for n in seq: append_timeline(n)
            update_ngrams()
        await ia2_process_once()
        return {"ok":True,"analise":True}

    if not is_real_entry(t):
        await ia2_process_once(); return {"ok":True,"skipped":True}

    source_msg_id=msg.get("message_id")
    if query_one("SELECT 1 FROM suggestions WHERE source_msg_id=?", (source_msg_id,)):
        await ia2_process_once(); return {"ok":True,"dup":True}

    strategy=extract_strategy(t) or ""
    seq_raw=extract_seq_raw(t) or ""
    after_num=extract_after_num(t)
    base,pattern_key=parse_bases_and_pattern(t)
    if not base: base=[1,2,3,4]; pattern_key="GEN"

    number,conf,samples,post=suggest_number(base, pattern_key, strategy, after_num)
    if number is None:
        await ia2_process_once(); return {"ok":True,"skipped_low_conf":True}

    exec_write("""INSERT OR REPLACE INTO suggestions
                  (source_msg_id,strategy,seq_raw,context_key,pattern_key,base,suggested_number,stage,sent_at)
                  VALUES (?,?,?,?,?,?,?,?,?)""",
               (source_msg_id, strategy, seq_raw, "CTX", pattern_key, json.dumps(base), int(number), "G0", now_ts()))
    exec_write("""INSERT OR REPLACE INTO last_by_strategy
                  (strategy,source_msg_id,suggested_number,context_key,pattern_key,stage,created_at)
                  VALUES (?,?,?,?,?,?,?)""",
               (strategy, source_msg_id, int(number), "CTX", pattern_key, "G0", now_ts()))
    open_pending(strategy, int(number), source="CHAN")
    await tg_broadcast(build_suggestion_msg(int(number), base, pattern_key, after_num, conf, samples, stage="G0"))
    await ia2_process_once()
    return {"ok":True,"sent":True,"number":number,"conf":conf,"samples":samples}

# ========= DEBUG =========
@app.get("/debug/samples")
async def debug_samples():
    r=query_one("SELECT SUM(weight) AS s FROM ngram_stats")
    samples=int((r["s"] or 0) if r else 0)
    return {"samples":samples,"MIN_SAMPLES":MIN_SAMPLES,"enough_samples":samples>=MIN_SAMPLES}

@app.get("/debug/reason")
async def debug_reason():
    try:
        age=max(0, now_ts()-(_ia2_last_reason_ts or now_ts()))
        return {"last_reason":_ia2_last_reason, "last_reason_age_seconds":age}
    except Exception as e: return {"error":str(e)}

@app.get("/debug/state")
async def debug_state():
    try:
        samples=int((_get_scalar("SELECT SUM(weight) FROM ngram_stats") or 0))
        pend_open=_get_scalar("SELECT COUNT(*) FROM pending_outcome WHERE open=1")
        reason_age=max(0, now_ts()-(_ia2_last_reason_ts or now_ts()))
        last_reason=_ia2_last_reason
        y=today_key_local()
        r=query_one("SELECT g0,loss,streak FROM daily_score WHERE yyyymmdd=?", (y,)); 
        g0=(r["g0"] if r else 0); loss=(r["loss"] if r else 0)
        acc=(g0/(g0+loss)) if (g0+loss)>0 else 0.0
        ria=query_one("SELECT g0,loss,streak FROM daily_score_ia WHERE yyyymmdd=?", (y,))
        ia_g0=(ria["g0"] if ria else 0); ia_loss=(ria["loss"] if ria else 0)
        ia_acc=(ia_g0/(ia_g0+ia_loss)) if (ia_g0+ia_loss)>0 else 0.0
        hb=_ia2_hour_key()
        cooldown_remaining=max(0, (_ia2_blocked_until_ts or 0)-now_ts())
        last_fire_age=max(0, now_ts()-(_ia2_last_fire_ts or 0))
        eff=_eff()
        cfg={"PHASE":eff["PHASE"],"MIN_SAMPLES":MIN_SAMPLES,"IA2_TIER_STRICT":eff["IA2_TIER_STRICT"],
             "IA2_DELTA_GAP":eff["IA2_DELTA_GAP"],"IA2_GAP_SAFETY":eff["IA2_GAP_SAFETY"],
             "IA2_MAX_PER_HOUR":eff["IA2_MAX_PER_HOUR"],"IA2_MIN_SECONDS_BETWEEN_FIRE":eff["IA2_MIN_SECONDS_BETWEEN_FIRE"],
             "IA2_COOLDOWN_AFTER_LOSS":eff["IA2_COOLDOWN_AFTER_LOSS"],"MAX_CONCURRENT_PENDINGS":eff["MAX_CONCURRENT_PENDINGS"],
             "INTEL_ANALYZE_INTERVAL":INTEL_ANALYZE_INTERVAL}
        _g7,_l7,acc7=_ia_acc_days(7)
        return {"samples":samples,"enough_samples":samples>=MIN_SAMPLES,"pendencias_abertas":int(pend_open),
                "last_reason":last_reason,"last_reason_age_seconds":int(reason_age),
                "hour_bucket":hb,"fires_enviados_nesta_hora":_ia2_sent_this_hour,
                "cooldown_pos_loss_seconds":int(cooldown_remaining),"ultimo_fire_ha_seconds":int(last_fire_age),
                "placar_canal_hoje":{"g0":int(g0),"loss":int(loss),"acc_pct":round(acc*100,2)},
                "placar_ia_hoje":{"g0":int(ia_g0),"loss":int(ia_loss),"acc_pct":round(ia_acc*100,2)},
                "ia_acc_7d":{"greens":_g7,"losses":_l7,"acc_pct":round(acc7*100,2)},"config":cfg}
    except Exception as e: return {"ok":False,"error":str(e)}

_last_flush_ts=0
@app.get("/debug/flush")
async def debug_flush(request:Request, days:int=7, key:str=""):
    global _last_flush_ts
    if not key or key!=FLUSH_KEY: return {"ok":False,"error":"unauthorized"}
    now=now_ts()
    if now-(_last_flush_ts or 0)<60:
        return {"ok":False,"error":"flush_cooldown","retry_after_seconds":60-(now-(_last_flush_ts or 0))}
    try: await tg_broadcast(_health_text())
    except Exception as e: print(f"[FLUSH] saúde: {e}")
    try: await tg_broadcast(_mk_relatorio_text(days=max(1,min(days,30))))
    except Exception as e: print(f"[FLUSH] relatório: {e}")
    _last_flush_ts=now
    return {"ok":True,"flushed":True,"days":max(1,min(days,30))}