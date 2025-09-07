import os
import time
import json
from collections import deque

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

from risk_engine import (
    winrate, volatilidade, streak_loss, risco_conf
)

BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "")
TARGET_CHAT = os.getenv("TARGET_CHAT", "")
CONF_LIMIAR = float(os.getenv("CONF_LIMIAR", "0.99"))
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "")

if not BOT_TOKEN:
    raise SystemExit("Faltou TG_BOT_TOKEN no ambiente.")

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

STATE_FILE = os.getenv("STATE_FILE", "data/state.json")
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

state = {"cooldown_until": 0, "limiar": CONF_LIMIAR}
try:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))
except Exception:
    pass

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

hist_long  = deque(maxlen=300)
hist_short = deque(maxlen=30)

def is_admin(user_id: int) -> bool:
    if not ADMIN_USER_ID:
        return True
    return str(user_id) == str(ADMIN_USER_ID)

def parse_signal(text: str) -> bool:
    up = text.upper()
    keys = ["SINAL", "ENTRADA", "CALL", "PUT"]
    return any(k in up for k in keys)

def parse_result(text: str):
    up = text.upper()
    if "GREEN" in up or "WIN" in up:
        return 1
    if "RED" in up or "LOSS" in up:
        return 0
    return None

async def publicar(texto: str):
    if TARGET_CHAT:
        await bot.send_message(TARGET_CHAT, texto)

@dp.message_handler(commands=["status"])
async def cmd_status(m: types.Message):
    short_wr = winrate(list(hist_short))
    long_wr  = winrate(list(hist_long))
    vol = volatilidade(list(hist_short))
    mxr = streak_loss(list(hist_short))
    await m.answer(
        f"📊 Short WR(30): {short_wr*100:.1f}% | Long WR(300): {long_wr*100:.1f}%\n"
        f"🔀 Volatilidade: {vol:.2f} | 📉 Max Reds: {mxr}\n"
        f"🎚️ Limiar atual: {state['limiar']*100:.2f}%"
    )

@dp.message_handler(content_types=types.ContentType.TEXT)
async def watcher(m: types.Message):
    chat_username = (m.chat.username and f"@{m.chat.username}") or ""
    in_source = True
    if SOURCE_CHANNEL:
        in_source = (chat_username.lower() == SOURCE_CHANNEL.lower())
    if not in_source:
        return

    text = (m.text or "").strip()
    if not text:
        return

    r = parse_result(text)
    if r is not None:
        hist_long.append(r); hist_short.append(r)
        return

    if parse_signal(text):
        if time.time() < state["cooldown_until"]:
            await publicar("neutro")
            return

        short_wr = winrate(list(hist_short))
        long_wr  = winrate(list(hist_long))
        vol = volatilidade(list(hist_short))
        mxr = streak_loss(list(hist_short))
        conf = risco_conf(short_wr, long_wr, vol, mxr)

        if conf >= state["limiar"]:
            msg = f"🎯 Chance: {conf*100:.1f}%\n🛡️ Risco: BAIXO\n📍 Ação: ENTRAR"
        else:
            msg = "neutro"
        await publicar(msg)

def main():
    executor.start_polling(dp, skip_updates=True)

if __name__ == "__main__":
    main()
