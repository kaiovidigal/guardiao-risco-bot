import os
import logging
from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.exceptions import ChatNotFound, Unauthorized

# --------------------------------------------------
# Config & logging
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("guardiao-risco-bot")

BOT_TOKEN = os.getenv("TG_BOT_TOKEN") or os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("TG_CHAT_ID")  # ex.: -10028105080717

if not BOT_TOKEN:
    raise RuntimeError("Faltando variável de ambiente TG_BOT_TOKEN (ou BOT_TOKEN).")
if not CHANNEL_ID:
    raise RuntimeError("Faltando variável de ambiente TG_CHAT_ID com o ID do canal.")

# converte para int (Render salva como string)
try:
    CHANNEL_ID = int(CHANNEL_ID)
except ValueError:
    raise RuntimeError("TG_CHAT_ID deve ser um número inteiro (ex.: -1001234567890).")

bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

# --------------------------------------------------
# Comando /start (privado)
# --------------------------------------------------
@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    await msg.answer(
        "✅ Bot ativo!\n"
        "Sou o Guardião de Risco.\n"
        "• Já estou ouvindo postagens do canal configurado.\n"
        "• Me mantenha como ADMIN do canal."
    )

# --------------------------------------------------
# Posts do canal (precisa ser ADMIN no canal)
# --------------------------------------------------
@dp.channel_post_handler()
async def on_channel_post(message: types.Message):
    """
    Dispara sempre que sair uma nova mensagem no canal onde o bot é admin.
    """
    try:
        # Log básico no console (aparece nos logs do Render)
        logger.info(
            "Nova mensagem no canal %s (%s): %s",
            message.chat.title, message.chat.id, message.text or "<sem texto>"
        )

        # Exemplo: se quiser reencaminhar para o MESMO canal ou outro chat/grupo,
        # defina TARGET_CHAT_ID nas variáveis de ambiente (opcional).
        target = os.getenv("TARGET_CHAT_ID")
        if target:
            try:
                target = int(target)
                await bot.copy_message(
                    chat_id=target,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
            except (ValueError, ChatNotFound, Unauthorized) as e:
                logger.error("Falha ao encaminhar para TARGET_CHAT_ID: %s", e)

    except Exception as e:
        logger.exception("Erro ao processar mensagem do canal: %s", e)

# --------------------------------------------------
# Entrada
# --------------------------------------------------
if __name__ == "__main__":
    # skip_updates=True evita “TerminatedByOtherGetUpdates”
    executor.start_polling(dp, skip_updates=True)