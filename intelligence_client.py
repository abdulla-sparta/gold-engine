"""
intelligence_client.py — GoldEngine ↔ Newsbot bridge

Polls the Django newsbot's /api/intelligence/ endpoint every N minutes
and caches the latest XAU signal in CONFIG["intelligence"].

CONFIG["intelligence"] shape:
{
    "available":   True,
    "bias":        "BULLISH" | "BEARISH" | "NEUTRAL",
    "score":       int,
    "confidence":  int (0-100),
    "reasons":     [str, ...],
    "price_note":  str,
    "narrative":   str,          # Claude's analyst note from newsbot
    "timestamp":   "2026-06-09 08:32 UTC",
    "fetched_at":  float,        # time.time() when we last fetched
    "stale":       bool,         # True if > 2h old
}
"""

import time
import threading
import logging
import requests
from config import CONFIG

log = logging.getLogger(__name__)

_EMPTY = {
    "available":  False,
    "bias":       "NONE",
    "score":      0,
    "confidence": 0,
    "reasons":    [],
    "price_note": "",
    "narrative":  "",
    "timestamp":  "",
    "fetched_at": 0,
    "stale":      True,
}


def _fetch() -> dict:
    url = CONFIG.get("newsbot_url", "")
    if not url:
        return _EMPTY.copy()

    endpoint = url.rstrip("/") + "/api/intelligence/"
    try:
        r = requests.get(endpoint, timeout=10)
        if not r.ok:
            log.warning(f"[Intelligence] HTTP {r.status_code} from newsbot")
            return _EMPTY.copy()

        data = r.json()
        if not data.get("available"):
            return _EMPTY.copy()

        now = time.time()
        # Mark stale if signal is > 2 hours old
        ts_str = data.get("timestamp", "")
        stale  = False
        try:
            from datetime import datetime, timezone
            ts_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            stale = (now - ts_dt.timestamp()) > 7200
        except Exception:
            pass

        return {
            "available":  True,
            "bias":       data.get("bias",       "NEUTRAL"),
            "score":      data.get("score",       0),
            "confidence": data.get("confidence",  50),
            "reasons":    data.get("reasons",     []),
            "price_note": data.get("price_note",  ""),
            "narrative":  data.get("narrative",   ""),
            "timestamp":  ts_str,
            "fetched_at": now,
            "stale":      stale,
        }

    except Exception as e:
        log.warning(f"[Intelligence] Fetch error: {e}")
        return _EMPTY.copy()


def _poll_loop():
    interval = CONFIG.get("intelligence_poll_interval", 1800)  # 30 min default
    log.info(f"[Intelligence] Poll loop started — interval={interval}s")

    while True:
        result = _fetch()
        CONFIG["intelligence"] = result
        if result["available"]:
            log.info(
                f"[Intelligence] Updated — Bias:{result['bias']} "
                f"Score:{result['score']} Conf:{result['confidence']}% "
                f"Stale:{result['stale']}"
            )
        else:
            log.debug("[Intelligence] Newsbot unavailable or no signal yet")
        time.sleep(interval)


def start_polling():
    """Fetch once immediately, then start background poll thread."""
    # Immediate first fetch so dashboard has data at startup
    CONFIG["intelligence"] = _EMPTY.copy()
    first = _fetch()
    CONFIG["intelligence"] = first
    if first["available"]:
        log.info(f"[Intelligence] Initial fetch — Bias:{first['bias']} Score:{first['score']}")
    else:
        log.info("[Intelligence] Initial fetch — no signal available (newsbot down or no data)")

    t = threading.Thread(target=_poll_loop, daemon=True, name="IntelPoller")
    t.start()
    return t
