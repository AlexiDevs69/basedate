"""
Minimal Telegram Bot API client used only for sending broadcast messages
from the admin dashboard. Deliberately dependency-light (just httpx) --
no bot framework needed here, we only ever call sendMessage.
"""

import httpx

TELEGRAM_API_BASE = "https://api.telegram.org"


async def send_telegram_message(
    bot_token: str,
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
    button_text: str | None = None,
    button_url: str | None = None,
) -> tuple[bool, str]:
    """
    Sends one message via the Telegram Bot API.
    Returns (success, error_message). error_message is "" on success.
    """
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}

    if parse_mode:
        payload["parse_mode"] = parse_mode

    if button_text and button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button_text, "url": button_url}]]
        }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
            if data.get("ok"):
                return True, ""
            return False, data.get("description", "Unknown Telegram API error")
    except Exception as exc:  # network errors, timeouts, etc.
        return False, str(exc)
