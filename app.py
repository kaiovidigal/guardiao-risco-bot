# app.py
import os
import requests
import threading, queue, time, hashlib
from fastapi import FastAPI, Request

# ========= Config =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "8315698154:AAH38hr2RbR0DtfalMNuXdGsh4UghDeztK4")
DEST_CHANNEL_ID = int(os.getenv("DEST_CHANNEL_ID", "-1002810508717"))  # canal destino
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "meusegredo123")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()

# ========= Dedup =========
SEEN_TTL = 120  # seg
_seen = {}
_seen_lock = threading.Lock()

def _dedup_key(chat_id: int, message_id: int, text: str) -> str:
    h = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest() if text else "no-text"
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

# ========= Fila assíncrona =========
q = queue.Queue()

def worker():
    while True:
        try:
            chat_id, text = q.get()
            if not text:
                q.task_done()
                continue
            requests.post(f"{TELEGRAM_API}/sendMessage", json={
                "chat_id": chat_id,
                "text": text
            }, timeout=15)
        except Exception:
            pass
        finally:
            q.task_done()

threading.Thread(target=worker, daemon=True).start()

# ========= Rotas =========
@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/health")
def health():
    return {"ok": True}

@app.post(f"/webhook/{WEBHOOK_TOKEN}")
async def telegram_webhook(request: Request):
    update = await request.json()

    def handle():
        try:
            data = update.get("channel_post") or update.get("message") or {}
            chat = data.get("chat") or {}
            chat_id = int(chat.get("id", 0))
            text = data.get("text") or ""

            if not chat_id or not text:
                return

            # não repassar o próprio destino
            if chat_id == DEST_CHANNEL_ID:
                return

            message_id = int(data.get("message_id", 0))
            key = _dedup_key(chat_id, message_id, text)
            if already_processed(key):
                return

            # filtro simples: só sinais
            is_sinal = "ENTRADA CONFIRMADA" in text or "🚨" in text or "ANALISANDO" in text
            if not is_sinal:
                return

            payload = text
            q.put((DEST_CHANNEL_ID, payload))

        except Exception:
            return

    threading.Thread(target=handle, daemon=True).start()
    return {"ok": True}