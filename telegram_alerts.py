"""
telegram_alerts.py — Telegram notification helper for GoldEngine
"""
import logging
import requests
from config import CONFIG

log = logging.getLogger(__name__)


def send_message(text: str) -> bool:
    bot_token = CONFIG.get("telegram_bot_token", "")
    chat_id   = CONFIG.get("telegram_chat_id", "")
    if not bot_token or not chat_id:
        log.warning(f"[Telegram] Not configured — message: {text[:80]}")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id":                  chat_id,
                "text":                     text,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not r.ok:
            log.warning(f"[Telegram] HTTP {r.status_code}: {r.text[:100]}")
            return False
        return True
    except Exception as e:
        log.warning(f"[Telegram] send failed: {e}")
        return False
