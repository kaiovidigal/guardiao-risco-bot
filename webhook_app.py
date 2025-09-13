import asyncio, httpx, os

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "COLOQUE_AQUI_SEU_TOKEN")
IA_FIRE_CHANNEL = "-1002796105884"
TELEGRAM_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

async def tg_send_fire(text: str, parse: str="HTML"):
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": IA_FIRE_CHANNEL,
                "text": text + "\n\nok",
                "parse_mode": parse,
                "disable_web_page_preview": True,
            },
        )

async def main():
    # Simula um sinal FIRE fake
    best, conf, samples = 3, 0.62, 1500
    txt = (
        f"🤖 <b>Tiro seco por IA [FIRE]</b>\n"
        f"🎯 Número seco (G0): <b>{best}</b>\n"
        f"📈 Conf: <b>{conf*100:.2f}%</b> | Amostra≈<b>{samples}</b>"
    )
    await tg_send_fire(txt)

if __name__ == "__main__":
    asyncio.run(main())