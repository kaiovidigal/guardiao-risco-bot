# app.py
import os, time, hashlib, threading, queue, json
from fastapi import FastAPI, Request
import requests

# Lê das duas chaves (prioriza BOT_TOKEN/DEST_CHANNEL_ID)
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TG_BOT_TOKEN")
DEST_CHANNEL_ID = os.getenv("DEST_CHANNEL_ID") or os.getenv("TARGET_CHANNEL")

if not BOT_TOKEN:
    raise RuntimeError("Defina BOT_TOKEN (ou TG_BOT_TOKEN) no ambiente.")
if not DEST_CHANNEL_ID:
    raise RuntimeError("Defina DEST_CHANNEL_ID (ou TARGET_CHANNEL) no ambiente.")

DEST_CHANNEL_ID = int(DEST_CHANNEL_ID)
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()

# ---- DEDUP SIMPLES COM TTL ----
SEEN_TTL = 120  # seg
_seen = {}
_seen_lock = threading.Lock()

def _dedup_key(chat_id: int, message_id: int, text: str) -> str:
    h = hashlib.sha1((text or "").encode("utf-8", errors="ignore")).hexdigest()
    return f"{chat_id}:{message_id}:{h}"

def already_processed(key: str) -> bool:
    now = time.time()
    with _seen_lock:
        for k, ts in list(_seen.items()):
            if now - ts > SEEN_TTL:
                _seen.pop(k, None)
        if key in _seen:
            return True
        _seen[key] = now
        return False

# ---- FILA ASSÍNCRONA ----
q = queue.Queue()

def worker():
    while True:
        try:
            chat_id, text = q.get()
            if text:
                r = requests.post(f"{TELEGRAM_API}/sendMessage",
                                  json={"chat_id": chat_id, "text": text},
                                  timeout=15)
                print("SEND resp:", r.status_code, r.text[:200])
        except Exception as e:
            print("SEND error:", repr(e))
        finally:
            q.task_done()

threading.Thread(target=worker, daemon=True).start()

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    print("INCOMING:", json.dumps(update, ensure_ascii=False)[:1000])

    def handle():
        try:
            data = (
                update.get("channel_post")
                or update.get("edited_channel_post")
                or update.get("message")
                or {}
            )
            chat = data.get("chat") or {}
            chat_id = int(chat.get("id", 0))
            text = data.get("text") or data.get("caption") or ""
            message_id = int(data.get("message_id", 0))

            if not chat_id or not text:
                return

            # anti-loop
            if chat_id == DEST_CHANNEL_ID:
                return

            # dedup
            key = _dedup_key(chat_id, message_id, text)
            if already_processed(key):
                return

            # *** sem filtro (encaminha tudo) — depois reativamos ***
            q.put((DEST_CHANNEL_ID, text))
            print("ENQUEUED:", {"to": DEST_CHANNEL_ID, "len": len(text)})
        except Exception as e:
            print("HANDLE error:", repr(e))
            return

    threading.Thread(target=handle, daemon=True).start()
    return {"ok": True}