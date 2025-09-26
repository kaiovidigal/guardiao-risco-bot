#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webhook_app.py — v4.4.2
- Fluxo estrito + Anti-tilt sem reduzir sinais + robustez de canal
- IA local (LLM) como 4º especialista (opcional)
- Suporte aos formatos: ENTRADA CONFIRMADA, APOSTA ENCERRADA (GREEN/RED), ANALISANDO
- GEN_AUTO habilitado por ENV
- Placar zera às 00:00 do TZ (default America/Sao_Paulo)
- Timeout desativado e anti-repetição imediata do mesmo número

ENV obrigatórias: TG_BOT_TOKEN, WEBHOOK_TOKEN
ENV opcionais:    TARGET_CHANNEL, SOURCE_CHANNEL, DB_PATH, DEBUG_MSG, BYPASS_SOURCE
                  LLM_ENABLED, LLM_MODEL_PATH, LLM_CTX_TOKENS, LLM_N_THREADS, LLM_TEMP, LLM_TOP_P
                  TZ_NAME, GEN_AUTO
Webhook:          POST /webhook/{WEBHOOK_TOKEN}
"""
import os, re, time, sqlite3, math, json
from contextlib import contextmanager
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone

# ---- timezone (reset diário) ----
TZ_NAME = os.getenv("TZ_NAME", "America/Sao_Paulo").strip()
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(TZ_NAME)
except Exception:
    _TZ = timezone.utc

import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= ENV =========
TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "").strip()
WEBHOOK_TOKEN  = os.getenv("WEBHOOK_TOKEN", "").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "-1002796105884").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "").strip()  # se vazio, não filtra
DB_PATH        = os.getenv("DB_PATH", "/var/data/data.db").strip() or "/var/data/data.db"
DEBUG_MSG      = os.getenv("DEBUG_MSG", "0").strip() in ("1","true","True","yes","YES")
BYPASS_SOURCE  = os.getenv("BYPASS_SOURCE", "0").strip() in ("1","true","True","yes","YES")
TELEGRAM_API   = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
GEN_AUTO       = os.getenv("GEN_AUTO", "1").strip() in ("1","true","True","yes","YES")

if not TG_BOT_TOKEN:  raise RuntimeError("Defina TG_BOT_TOKEN no ambiente.")
if not WEBHOOK_TOKEN: raise RuntimeError("Defina WEBHOOK_TOKEN no ambiente.")

# ========= App =========
app = FastAPI(title="guardiao-auto-bot (GEN webhook)", version="4.4.2")

# ========= Parâmetros =========
DECAY = 0.980
W4, W3, W2, W1 = 0.42, 0.30, 0.18, 0.10
OBS_TIMEOUT_SEC = 10**9  # desativado: só fecha com observados reais
CONF_MIN, GAP_MIN, H_MAX = 0.55, 0.08, 0.80
COOLDOWN_N = 2
ALWAYS_ENTER = True

# ======== Online Learning (feedback) ========
FEED_BETA   = 0.45
FEED_POS    = 1.0
FEED_NEG    = 1.0
FEED_DECAY  = 0.995
WF4, WF3, WF2, WF1 = W4, W3, W2, W1

# ======== Ensemble Hedge ========
HEDGE_ETA = 0.6
K_SHORT   = 60
K_LONG    = 300

# ========= Utils =========
def now_ts() -> int: return int(time.time())
def tz_today_ymd() -> str: return datetime.now(_TZ).strftime("%Y-%m-%d")

# ========= DB =========
def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=10000;")
    return con

@contextmanager
def _tx():
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except Exception:
        con.rollback(); raise
    finally:
        con.close()

def migrate_db():
    con = _connect(); cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, number INTEGER NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ngram (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, nxt INTEGER NOT NULL, w REAL NOT NULL,
        PRIMARY KEY (n, ctx, nxt))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER, suggested INTEGER, stage INTEGER DEFAULT 0,
        open INTEGER DEFAULT 1, seen TEXT, opened_at INTEGER, "after" INTEGER,
        ctx1 TEXT, ctx2 TEXT, ctx3 TEXT, ctx4 TEXT, wait_notice_sent INTEGER DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS score (
        id INTEGER PRIMARY KEY CHECK (id=1), green INTEGER DEFAULT 0, loss INTEGER DEFAULT 0)""")
    if not con.execute("SELECT 1 FROM score WHERE id=1").fetchone():
        cur.execute("INSERT INTO score (id, green, loss) VALUES (1,0,0)")
    cur.execute("""CREATE TABLE IF NOT EXISTS feedback (
        n INTEGER NOT NULL, ctx TEXT NOT NULL, nxt INTEGER NOT NULL, w REAL NOT NULL,
        PRIMARY KEY (n, ctx, nxt))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS processed (
        update_id TEXT PRIMARY KEY, seen_at INTEGER NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS state (
        id INTEGER PRIMARY KEY CHECK (id=1),
        cooldown_left INTEGER DEFAULT 0, loss_streak INTEGER DEFAULT 0,
        last_reset_ymd TEXT DEFAULT '', last_suggested INTEGER DEFAULT -1)""")
    if not con.execute("SELECT 1 FROM state WHERE id=1").fetchone():
        cur.execute("INSERT INTO state (id,cooldown_left,loss_streak,last_reset_ymd,last_suggested) VALUES (1,0,0,'',-1)")
    con.commit(); con.close()
migrate_db()

def _exec_write(sql: str, params: tuple=()):
    for attempt in range(6):
        try:
            with _tx() as con: con.execute(sql, params)
            return
        except sqlite3.OperationalError as e:
            if any(k in str(e).lower() for k in ("locked","busy")):
                time.sleep(0.25*(attempt+1)); continue
            raise

# ========= Dedupe =========
def _is_processed(update_id: str) -> bool:
    if not update_id: return False
    con = _connect(); row = con.execute("SELECT 1 FROM processed WHERE update_id=?", (update_id,)).fetchone(); con.close()
    return bool(row)
def _mark_processed(update_id: str):
    if update_id: _exec_write("INSERT OR IGNORE INTO processed (update_id, seen_at) VALUES (?,?)", (str(update_id), now_ts()))

# ========= Score/reset =========
def bump_score(outcome: str) -> Tuple[int, int]:
    with _tx() as con:
        row = con.execute("SELECT green, loss FROM score WHERE id=1").fetchone()
        g, l = (row["green"], row["loss"]) if row else (0,0)
        if outcome.upper()=="GREEN": g+=1
        elif outcome.upper()=="LOSS": l+=1
        con.execute("INSERT OR REPLACE INTO score (id, green, loss) VALUES (1,?,?)", (g,l)); return g,l

def reset_score(): _exec_write("INSERT OR REPLACE INTO score (id, green, loss) VALUES (1,0,0)")
def score_text() -> str:
    con = _connect(); row = con.execute("SELECT green, loss FROM score WHERE id=1").fetchone(); con.close()
    g,l = (int(row["green"]), int(row["loss"])) if row else (0,0)
    acc = (g/(g+l)*100.0) if (g+l)>0 else 0.0
    return f"{g} GREEN × {l} LOSS — {acc:.1f}%"
def _get_last_reset_ymd() -> str:
    con=_connect(); row=con.execute("SELECT last_reset_ymd FROM state WHERE id=1").fetchone(); con.close()
    return (row["last_reset_ymd"] or "") if row else ""
def _set_last_reset_ymd(ymd:str): _exec_write("UPDATE state SET last_reset_ymd=? WHERE id=1",(ymd,))
def check_and_maybe_reset_score():
    today = tz_today_ymd(); last = _get_last_reset_ymd()
    if last != today: reset_score(); _set_last_reset_ymd(today)

# ========= State =========
def _get_cooldown()->int:
    con=_connect(); row=con.execute("SELECT cooldown_left FROM state WHERE id=1").fetchone(); con.close()
    return int((row["cooldown_left"] if row else 0) or 0)
def _set_cooldown(v:int): _exec_write("UPDATE state SET cooldown_left=? WHERE id=1",(int(v),))
def _dec_cooldown():
    with _tx() as con:
        row=con.execute("SELECT cooldown_left FROM state WHERE id=1").fetchone()
        cur=int((row["cooldown_left"] if row else 0) or 0); cur=max(0,cur-1)
        con.execute("UPDATE state SET cooldown_left=? WHERE id=1",(cur,))
def _get_loss_streak()->int:
    con=_connect(); row=con.execute("SELECT loss_streak FROM state WHERE id=1").fetchone(); con.close()
    return int((row["loss_streak"] if row else 0) or 0)
def _set_loss_streak(v:int): _exec_write("UPDATE state SET loss_streak=? WHERE id=1",(int(v),))
def _bump_loss_streak(reset: bool):
    if reset: _set_loss_streak(0)
    else: _set_loss_streak(_get_loss_streak()+1)
def _get_last_suggested()->int:
    con=_connect(); row=con.execute("SELECT last_suggested FROM state WHERE id=1").fetchone(); con.close()
    try: return int(row["last_suggested"])
    except Exception: return -1
def _set_last_suggested(v:int): _exec_write("UPDATE state SET last_suggested=? WHERE id=1",(int(v),))

# ========= Timeline / n-gram / feedback =========
def timeline_size()->int:
    con=_connect(); row=con.execute("SELECT COUNT(*) AS c FROM timeline").fetchone(); con.close()
    return int(row["c"] or 0)
def get_tail(limit:int=400)->List[int]:
    con=_connect(); rows=con.execute("SELECT number FROM timeline ORDER BY id DESC LIMIT ?",(limit,)).fetchall(); con.close()
    return [int(r["number"]) for r in rows][::-1]
def append_seq(seq:List[int]):
    if not seq: return
    with _tx() as con:
        for n in seq: con.execute("INSERT INTO timeline (created_at, number) VALUES (?,?)",(now_ts(),int(n)))
    _update_ngrams()
def _update_ngrams(decay:float=DECAY, max_n:int=5, window:int=400):
    tail=get_tail(window); if len(tail)<2: return
    with _tx() as con:
        for t in range(1,len(tail)):
            nxt=int(tail[t]); dist=(len(tail)-1)-t; w=decay**dist
            for n in range(2,max_n+1):
                if t-(n-1)<0: break
                ctx=tail[t-(n-1):t]; ctx_key=",".join(str(x) for x in ctx)
                con.execute("""INSERT INTO ngram (n,ctx,nxt,w) VALUES (?,?,?,?)
                               ON CONFLICT(n,ctx,nxt) DO UPDATE SET w=w+excluded.w""",(n,ctx_key,nxt,float(w)))
def _prob_from_ngrams(ctx:List[int], cand:int)->float:
    n=len(ctx)+1
    if n<2 or n>5: return 0.0
    ctx_key=",".join(str(x) for x in ctx)
    con=_connect()
    row_tot=con.execute("SELECT SUM(w) AS s FROM ngram WHERE n=? AND ctx=?",(n,ctx_key)).fetchone()
    tot=(row_tot["s"] or 0.0) if row_tot else 0.0
    if tot<=0: con.close(); return 0.0
    row_c=con.execute("SELECT w FROM ngram WHERE n=? AND ctx=? AND nxt=?",(n,ctx_key,int(cand))).fetchone()
    w=(row_c["w"] or 0.0) if row_c else 0.0
    con.close(); return w/(tot or 1e-9)

def _ctx_to_key(ctx:List[int])->str: return ",".join(str(x) for x in ctx) if ctx else ""
def _feedback_upsert(n:int, ctx_key:str, nxt:int, delta:float):
    with _tx() as con:
        con.execute("UPDATE feedback SET w = w * ?", (FEED_DECAY,))
        con.execute("""INSERT INTO feedback (n,ctx,nxt,w) VALUES (?,?,?,?)
                       ON CONFLICT(n,ctx,nxt) DO UPDATE SET w=w+excluded.w""",
                    (n,ctx_key,int(nxt),float(delta)))
def _feedback_prob(n:int, ctx:List[int], cand:int)->float:
    if not ctx: return 0.0
    ctx_key=_ctx_to_key(ctx); con=_connect()
    row_tot=con.execute("SELECT SUM(w) AS s FROM feedback WHERE n=? AND ctx=?",(n,ctx_key)).fetchone()
    tot=(row_tot["s"] or 0.0) if row_tot else 0.0
    if tot<=0: con.close(); return 0.0
    row_c=con.execute("SELECT w FROM feedback WHERE n=? AND ctx=? AND nxt=?",(n,ctx_key,int(cand))).fetchone()
    w=(row_c["w"] or 0.0) if row_c else 0.0; con.close()
    tot=abs(tot); return max(0.0,w)/(tot if tot>0 else 1e-9)

# ========= LLM local (opcional) =========
try:
    from llama_cpp import Llama
    _LLM=None
    def _llm_load():
        global _LLM
        if _LLM is None and os.path.exists(LLM_MODEL_PATH:=os.getenv("LLM_MODEL_PATH","models/phi-3-mini.gguf")):
            _LLM=Llama(model_path=LLM_MODEL_PATH,
                       n_ctx=int(os.getenv("LLM_CTX_TOKENS","2048")),
                       n_threads=int(os.getenv("LLM_N_THREADS","4")),
                       verbose=False)
        return _LLM
except Exception:
    _LLM=None
    def _llm_load(): return None
LLM_ENABLED = os.getenv("LLM_ENABLED","1").strip() in ("1","true","True","yes","YES")
LLM_TEMP, LLM_TOP_P = float(os.getenv("LLM_TEMP","0.2")), float(os.getenv("LLM_TOP_P","0.95"))
_LLM_SYSTEM=("Você é um assistente que prevê o próximo número {1,2,3,4}. "
             "Responda APENAS JSON: {\"1\":p1,\"2\":p2,\"3\":p3,\"4\":p4}")
def _norm_dict(d:Dict[int,float])->Dict[int,float]:
    s=sum(d.values()) or 1e-9; return {k:v/s for k,v in d.items()}
def _post_freq_k(tail:List[int], k:int)->Dict[int,float]:
    if not tail: return {1:0.25,2:0.25,3:0.25,4:0.25}
    win=tail[-k:] if len(tail)>=k else tail; tot=max(1,len(win))
    return _norm_dict({c:win.count(c)/tot for c in (1,2,3,4)})
def _llm_probs_from_tail(tail:List[int])->Dict[int,float]:
    if not (LLM_ENABLED and _llm_load()): return {}
    win60=tail[-60:] if len(tail)>=60 else tail
    win300=tail[-300:] if len(tail)>=300 else tail
    def freq(win,n): return win.count(n)/max(1,len(win))
    feats={"len":len(tail),"last10":tail[-10:],
           "freq60":{n:freq(win60,n) for n in (1,2,3,4)},
           "freq300":{n:freq(win300,n) for n in (1,2,3,4)}}
    user=(f"Historico_Recente={tail[-50:]}\nFeats={feats}\n"
          "Devolva JSON {\"1\":p1,\"2\":p2,\"3\":p3,\"4\":p4}")
    try:
        out=_LLM.create_chat_completion(messages=[{"role":"system","content":_LLM_SYSTEM},
                                                  {"role":"user","content":user}],
                                        temperature=LLM_TEMP, top_p=LLM_TOP_P, max_tokens=128)
        text=out["choices"][0]["message"]["content"].strip()
        m=re.search(r"\{.*\}", text, re.S); data=json.loads(m.group(0) if m else text)
        raw={int(k):float(v) for k,v in data.items() if str(k) in ("1","2","3","4")}
        S=sum(raw.values()) or 1e-9; return {k:max(0.0,v/S) for k,v in raw.items() if k in (1,2,3,4)}
    except Exception: return {}

def _hedge_blend4(p1,p2,p3,p4):
    w1,w2,w3,w4=_get_expert_w(); S=(w1+w2+w3+w4) or 1e-9
    w1,w2,w3,w4=(w1/S,w2/S,w3/S,w4/S)
    blended={c:w1*p1.get(c,0)+w2*p2.get(c,0)+w3*p3.get(c,0)+w4*p4.get(c,0) for c in (1,2,3,4)}
    s2=sum(blended.values()) or 1e-9; return ({k:v/s2 for k,v in blended.items()}, (w1,w2,w3,w4))
def _get_expert_w():
    con=_connect(); row=con.execute("SELECT w1,w2,w3,w4 FROM expert_w WHERE id=1").fetchone(); con.close()
    return (float(row["w1"]),float(row["w2"]),float(row["w3"]),float(row["w4"])) if row else (1.0,1.0,1.0,1.0)
def _set_expert_w(w1,w2,w3,w4): _exec_write("UPDATE expert_w SET w1=?,w2=?,w3=?,w4=? WHERE id=1",(float(w1),float(w2),float(w3),float(w4)))
def _hedge_update4(true_c,p1,p2,p3,p4):
    w1,w2,w3,w4=_get_expert_w(); l=lambda p:1.0-p.get(true_c,0.0); from math import exp
    w1n=w1*exp(-HEDGE_ETA*(1.0-l(p1))); w2n=w2*exp(-HEDGE_ETA*(1.0-l(p2)))
    w3n=w3*exp(-HEDGE_ETA*(1.0-l(p3))); w4n=w4*exp(-HEDGE_ETA*(1.0-l(p4)))
    S=(w1n+w2n+w3n+w4n) or 1e-9; _set_expert_w(w1n/S,w2n/S,w3n/S,w4n/S)

def _streak_adjust_choice(post:Dict[int,float], gap:float, ls:int)->Tuple[int,str,Dict[int,float]]:
    reason="IA"; ranking=sorted(post.items(), key=lambda kv: kv[1], reverse=True); best=ranking[0][0]
    if ls>=3:
        comp=_norm_dict({c:max(1e-9,1.0-post[c]) for c in (1,2,3,4)})
        post=_norm_dict({c:0.7*post[c]+0.3*comp[c] for c in (1,2,3,4)}); ranking=sorted(post.items(), key=lambda kv: kv[1], reverse=True)
        best=ranking[0][0]; reason="IA_anti_tilt_mix"
    if ls>=2 and len(ranking)>=2 and gap<0.05:
        best=ranking[1][0]; reason="IA_runnerup_ls2"
    return best, reason, post

def _prob_post_from_tail(tail:List[int], after:Optional[int])->Dict[int,float]:
    cands=[1,2,3,4]; scores={c:0.0 for c in cands}
    if not tail: return {c:0.25 for c in cands}
    if after is not None and after in tail:
        idxs=[i for i,v in enumerate(tail) if v==after]; i=idxs[-1]
        ctx1=tail[max(0,i):i+1]; ctx2=tail[max(0,i-1):i+1] if i-1>=0 else []
        ctx3=tail[max(0,i-2):i+1] if i-2>=0 else []; ctx4=tail[max(0,i-3):i+1] if i-3>=0 else []
    else:
        ctx4=tail[-4:] if len(tail)>=4 else []; ctx3=tail[-3:] if len(tail)>=3 else []
        ctx2=tail[-2:] if len(tail)>=2 else []; ctx1=tail[-1:] if len(tail)>=1 else []
    for c in cands:
        s=0.0
        if len(ctx4)==4: s+=W4*_prob_from_ngrams(ctx4[:-1],c)
        if len(ctx3)==3: s+=W3*_prob_from_ngrams(ctx3[:-1],c)
        if len(ctx2)==2: s+=W2*_prob_from_ngrams(ctx2[:-1],c)
        if len(ctx1)==1: s+=W1*_prob_from_ngrams(ctx1[:-1],c)
        if len(ctx4)==4: s+=FEED_BETA*WF4*_feedback_prob(4,ctx4[:-1],c)
        if len(ctx3)==3: s+=FEED_BETA*WF3*_feedback_prob(3,ctx3[:-1],c)
        if len(ctx2)==2: s+=FEED_BETA*WF2*_feedback_prob(2,ctx2[:-1],c)
        if len(ctx1)==1: s+=FEED_BETA*WF1*_feedback_prob(1,ctx1[:-1],c)
        scores[c]=s
    tot=sum(scores.values()) or 1e-9; return {k:v/tot for k,v in scores.items()}

def choose_single_number(after:Optional[int]):
    tail=get_tail(400)
    post_e1=_prob_post_from_tail(tail,after)
    post_e2=_post_freq_k(tail,K_SHORT)
    post_e3=_post_freq_k(tail,K_LONG)
    post_e4=_llm_probs_from_tail(tail) or {1:0.25,2:0.25,3:0.25,4:0.25}
    post,(w1,w2,w3,w4)=_hedge_blend4(post_e1,post_e2,post_e3,post_e4)
    ranking=sorted(post.items(), key=lambda kv: kv[1], reverse=True)
    top2=ranking[:2]; gap=(top2[0][1]-top2[1][1]) if len(top2)>=2 else ranking[0][1]
   # ========= Telegram =========
async def tg_send_text(chat_id: str, text: str, parse: str = "HTML"):
    if not TG_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": parse, "disable_web_page_preview": True},
            )
    except Exception:
        pass

# ========= Regex/Parsers (formatos do seu fonte) =========
KEYCAP_MAP = {"1️⃣": "1", "2️⃣": "2", "3️⃣": "3", "4️⃣": "4"}
def _normalize_keycaps(s: str) -> str:
    return "".join(KEYCAP_MAP.get(ch, ch) for ch in (s or ""))

ENTRY_RX = re.compile(r"\bENTRADA\s+CONFIRMADA\b", re.I)
SEQ_RX   = re.compile(r"Sequ[eê]ncia:\s*([^\n\r]+)", re.I)
AFTER_RX = re.compile(r"ap[oó]s\s+o\s+([1-4])", re.I)

CLOSE_TAG_RX  = re.compile(r"(APOSTA\s+ENCERRADA|GREEN|WIN|✅|LOSS|RED|❌)", re.I)
CLOSE_NUMS_RX = re.compile(r"\(([^)]*)\)")

ANALISANDO_RX = re.compile(r"\bANALISANDO\b", re.I)

def parse_entry_text(text: str):
    t = _normalize_keycaps(re.sub(r"\s+", " ", text or "").strip())
    if not ENTRY_RX.search(t):
        return None
    seq = []
    mseq = SEQ_RX.search(t)
    if mseq:
        seq = [int(x) for x in re.findall(r"[1-4]", _normalize_keycaps(mseq.group(1)))]
    mafter = AFTER_RX.search(t)
    after_num = int(mafter.group(1)) if mafter else None
    return {"seq": seq, "after": after_num, "raw": t}

def parse_close_numbers(text: str):
    t = _normalize_keycaps(re.sub(r"\s+", " ", text or ""))
    if not CLOSE_TAG_RX.search(t):
        return []
    mg = CLOSE_NUMS_RX.findall(t)
    if mg:
        last = mg[-1]
        nums = re.findall(r"[1-4]", _normalize_keycaps(last))
        return [int(x) for x in nums][:3]
    nums = re.findall(r"[1-4]", t)
    return [int(x) for x in nums][:3]

def parse_analise_seq(text: str):
    t = _normalize_keycaps(text or "")
    if not ANALISANDO_RX.search(t):
        return []
    mseq = SEQ_RX.search(t)
    if not mseq:
        return []
    return [int(x) for x in re.findall(r"[1-4]", _normalize_keycaps(mseq.group(1)))]

# ========= Pending helpers =========
def get_open_pending() -> Optional[sqlite3.Row]:
    con = _connect()
    row = con.execute("SELECT * FROM pending WHERE open=1 ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return row

def set_stage(stage: int):
    with _tx() as con:
        con.execute("UPDATE pending SET stage=? WHERE open=1", (int(stage),))

def _seen_list(row: sqlite3.Row) -> List[str]:
    seen = (row["seen"] or "").strip()
    return [s for s in seen.split("-") if s]

def _seen_append(row: sqlite3.Row, new_items: List[str]):
    cur_seen = _seen_list(row)
    for it in new_items:
        if len(cur_seen) >= 3:
            break
        if it not in cur_seen:
            cur_seen.append(it)
    seen_txt = "-".join(cur_seen[:3])
    with _tx() as con:
        con.execute("UPDATE pending SET seen=? WHERE id=?", (seen_txt, int(row["id"])))

def _stage_from_observed(suggested: int, obs: List[int]) -> Tuple[str, str]:
    if not obs:
        return ("LOSS", "G2")
    if len(obs) >= 1 and obs[0] == suggested: return ("GREEN", "G0")
    if len(obs) >= 2 and obs[1] == suggested: return ("GREEN", "G1")
    if len(obs) >= 3 and obs[2] == suggested: return ("GREEN", "G2")
    return ("LOSS", "G2")

def _ngram_snapshot_text(suggested: int) -> str:
    tail = get_tail(400)
    post = _prob_post_from_tail(tail, after=None)
    def pct(x: float) -> str:
        try: return f"{x*100:.1f}%"
        except Exception: return "0.0%"
    p1 = pct(post.get(1, 0.0)); p2 = pct(post.get(2, 0.0))
    p3 = pct(post.get(3, 0.0)); p4 = pct(post.get(4, 0.0))
    conf = pct(post.get(int(suggested), 0.0))
    amostra = timeline_size()
    return f"📈 Amostra: {amostra} • Conf: {conf}\n\n🔎 E1(n-gram+fb): 1 {p1} | 2 {p2} | 3 {p3} | 4 {p4}"

def _decision_context(after: Optional[int]) -> Tuple[List[int], List[int], List[int], List[int]]:
    tail = get_tail(400)
    if tail and after is not None and after in tail:
        idxs = [i for i,v in enumerate(tail) if v == after]
        i = idxs[-1]
        ctx1 = tail[max(0,i):i+1]
        ctx2 = tail[max(0,i-1):i+1] if i-1>=0 else []
        ctx3 = tail[max(0,i-2):i+1] if i-2>=0 else []
        ctx4 = tail[max(0,i-3):i+1] if i-3>=0 else []
    else:
        ctx4 = tail[-4:] if len(tail)>=4 else []
        ctx3 = tail[-3:] if len(tail)>=3 else []
        ctx2 = tail[-2:] if len(tail)>=2 else []
        ctx1 = tail[-1:] if len(tail)>=1 else []
    return ctx1, ctx2, ctx3, ctx4

def _open_pending_with_ctx(suggested:int, after:Optional[int], ctx1,ctx2,ctx3,ctx4) -> bool:
    with _tx() as con:
        row = con.execute("SELECT 1 FROM pending WHERE open=1 LIMIT 1").fetchone()
        if row: return False
        con.execute("""INSERT INTO pending (created_at, suggested, stage, open, seen, opened_at, "after",
                        ctx1, ctx2, ctx3, ctx4, wait_notice_sent)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (now_ts(), int(suggested), 0, 1, "", now_ts(), after,
                     ",".join(map(str,ctx1)), ",".join(map(str,ctx2)),
                     ",".join(map(str,ctx3)), ",".join(map(str,ctx4))))
        return True

def _close_with_outcome(row: sqlite3.Row, outcome: str, final_seen: str, stage_lbl: str, suggested: int):
    our_num_display = suggested if outcome.upper()=="GREEN" else "X"
    with _tx() as con:
        con.execute("UPDATE pending SET open=0, seen=? WHERE id=?", (final_seen, int(row["id"])))
    bump_score(outcome.upper())
    try:
        if outcome.upper()=="LOSS":
            _set_cooldown(COOLDOWN_N); _bump_loss_streak(reset=False)
        else:
            _dec_cooldown(); _bump_loss_streak(reset=True)
    except Exception:
        pass
    # timeline + feedback
    try:
        seen_add = [int(x) for x in final_seen.split("-") if x.isdigit()]
        append_seq(seen_add)
    except Exception:
        pass
    snapshot = _ngram_snapshot_text(int(suggested))
    msg = (f"{'🟢' if outcome.upper()=='GREEN' else '🔴'} <b>{outcome.upper()}</b> — finalizado "
           f"(<b>{stage_lbl}</b>, nosso={our_num_display}, observados={final_seen}).\n"
           f"📊 Geral: {score_text()}\n\n{snapshot}")
    return msg

def _maybe_close_by_timeout():
    # desativado (mantém por compatibilidade)
    return None

# ========= Rotas =========
@app.get("/")
async def root():
    check_and_maybe_reset_score()
    return {"ok": True, "service": "guardiao-auto-bot (GEN webhook)"}

@app.get("/health")
async def health():
    check_and_maybe_reset_score()
    pend = get_open_pending()
    return {"ok": True, "db": DB_PATH, "pending_open": bool(pend),
            "pending_seen": (pend["seen"] if pend else ""), "time": datetime.now(_TZ).isoformat(), "tz": TZ_NAME}

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        check_and_maybe_reset_score()
        data = await request.json()

        upd_id = str(data.get("update_id", "")) if isinstance(data, dict) else ""
        if _is_processed(upd_id):
            return {"ok": True, "skipped": "duplicate_update"}
        _mark_processed(upd_id)

        msg = data.get("channel_post") or data.get("message") \
            or data.get("edited_channel_post") or data.get("edited_message") or {}
        text = (msg.get("text") or msg.get("caption") or "").strip()
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")

        # filtro de fonte
        if SOURCE_CHANNEL and not BYPASS_SOURCE and chat_id != str(SOURCE_CHANNEL):
            if DEBUG_MSG:
                await tg_send_text(TARGET_CHANNEL, f"DEBUG: Ignorando chat {chat_id}. Fonte esperada: {SOURCE_CHANNEL}")
            return {"ok": True, "skipped": "outro_chat"}
        if not text:
            return {"ok": True, "skipped": "sem_texto"}

        # ANALISANDO (aprende sequência)
        if ANALISANDO_RX.search(_normalize_keycaps(text)):
            seq = parse_analise_seq(text)
            if seq: append_seq(seq)
            return {"ok": True, "analise_seen": len(seq)}

        # Fechamento
        if CLOSE_TAG_RX.search(_normalize_keycaps(text)):
            pend = get_open_pending()
            if pend:
                nums = parse_close_numbers(text)
                if nums:
                    _seen_append(pend, [str(n) for n in nums])
                    pend = get_open_pending()
                seen_list = _seen_list(pend) if pend else []
                if pend and len(seen_list) >= 3:
                    suggested = int(pend["suggested"] or 0)
                    obs_nums = [int(x) for x in seen_list if x.isdigit()]
                    outcome, stage_lbl = _stage_from_observed(suggested, obs_nums)
                    final_seen = "-".join(seen_list[:3])
                    out_msg = _close_with_outcome(pend, outcome, final_seen, stage_lbl, suggested)
                    await tg_send_text(TARGET_CHANNEL, out_msg)
                    return {"ok": True, "closed": outcome.lower(), "seen": final_seen}
            return {"ok": True, "noted_close": True}

        # ENTRADA
        parsed = parse_entry_text(text)
        if not parsed:
            if DEBUG_MSG:
                await tg_send_text(TARGET_CHANNEL, "DEBUG: Mensagem não reconhecida como ENTRADA/FECHAMENTO.")
            return {"ok": True, "skipped": "nao_eh_entrada_confirmada"}

        # se já existe pendência aberta, não abre outra
        pend = get_open_pending()
        if pend:
            seen_list = _seen_list(pend)
            if len(seen_list) >= 3:
                suggested = int(pend["suggested"] or 0)
                obs_nums = [int(x) for x in seen_list if x.isdigit()]
                outcome, stage_lbl = _stage_from_observed(suggested, obs_nums)
                final_seen = "-".join(seen_list[:3])
                out_msg = _close_with_outcome(pend, outcome, final_seen, stage_lbl, suggested)
                await tg_send_text(TARGET_CHANNEL, out_msg)
            else:
                return {"ok": True, "kept_open_waiting_close": True}

        seq = parsed["seq"] or []
        if seq: append_seq(seq)
        after = parsed["after"]

        best, conf, samples, post, gap, reason = choose_single_number(after)

        # anti-repetição imediata
        last_sug = _get_last_suggested()
        if best == last_sug:
            ranking = sorted(post.items(), key=lambda kv: kv[1], reverse=True)
            alt = next((c for c,_ in ranking if c != last_sug), best)
            best = alt
            conf = float(post.get(best, conf))
            reason = (reason + "_avoid_repeat")[:64]

        # abrir pending
        ctx1, ctx2, ctx3, ctx4 = _decision_context(after)
        if not _open_pending_with_ctx(best, after, ctx1, ctx2, ctx3, ctx4):
            if DEBUG_MSG:
                await tg_send_text(TARGET_CHANNEL, "DEBUG: Já existe pending open — não abri novo.")
            return {"ok": True, "skipped": "pending_already_open"}

        _set_last_suggested(best)
        aft_txt = f" após {after}" if after else ""
        prefix = "🤖 <b>IA AUTO</b> — " if GEN_AUTO else "🤖 "
        txt = (f"{prefix}<b>SUGERE</b> — <b>{best}</b>\n"
               f"🧩 <b>Padrão:</b> GEN{aft_txt}\n"
               f"📊 <b>Conf:</b> {conf*100:.2f}% | <b>Amostra≈</b>{samples} | <b>gap≈</b>{gap*100:.1f}pp")
        await tg_send_text(TARGET_CHANNEL, txt)
        return {"ok": True, "posted": True, "best": best, "conf": conf, "gap": gap, "samples": samples}

    except Exception as e:
        if DEBUG_MSG:
            try:
                await tg_send_text(TARGET_CHANNEL, f"⚠️ EXCEPTION no /webhook: <code>{type(e).__name__}: {str(e)[:400]}</code>")
            except Exception:
                pass
        print("EXCEPTION in /webhook:", repr(e))
        return {"ok": True, "error": type(e).__name__}