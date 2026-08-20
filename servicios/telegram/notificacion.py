import httpx, os
import logging
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_GROUP_ID")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

async def send_telegram_alert(message: str, parse_mode: str = "HTML"):
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode
    }
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(TELEGRAM_API_URL, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logging.error(f"Error enviando mensaje a Telegram: {exc.response.text}")
        except Exception as exc:
            logging.error(f"Fallo de conexión con Telegram: {exc}")