"""
macro_calendar.py — GoldEngine Macro Events Calendar
Fetches upcoming high-impact macro events from multiple free sources:
  1. Finnhub economic calendar API (free tier, no key needed for basic)
  2. Fed RSS feed (federalreserve.gov)
  3. Static forward schedule (hardcoded upcoming known dates)

Cached in memory for 1 hour to avoid hammering sources.
"""

import logging
import time
import threading
from datetime import datetime, timezone, timedelta

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

_cache = {"events": [], "fetched_at": 0}
_lock  = threading.Lock()

CACHE_TTL = 3600  # 1 hour

# ── Impact colors / labels ────────────────────────────────────────────────────
IMPACT_META = {
    "HIGH":   {"color": "#EF4444", "bg": "#FEF2F2", "border": "#FECACA"},
    "MEDIUM": {"color": "#F59E0B", "bg": "#FFFBEB", "border": "#FDE68A"},
    "LOW":    {"color": "#6B7280", "bg": "#F9FAFB", "border": "#E5E7EB"},
}

# ── Static forward calendar (always reliable as fallback) ─────────────────────
# Format: (yyyy, mm, dd, hh_utc, mm_utc, "Title", "HIGH/MEDIUM/LOW")
# Keep this updated monthly — acts as guaranteed baseline when APIs fail.
STATIC_EVENTS = [
    # FOMC 2025–2026 dates (all 8 per year, 2pm ET = 18:30 UTC)
    (2025,  7, 30, 18, 0, "FOMC Rate Decision",                      "HIGH"),
    (2025,  9, 17, 18, 0, "FOMC Rate Decision",                      "HIGH"),
    (2025, 10, 29, 18, 0, "FOMC Rate Decision",                      "HIGH"),
    (2025, 12, 10, 19, 0, "FOMC Rate Decision",                      "HIGH"),
    (2026,  1, 28, 19, 0, "FOMC Rate Decision",                      "HIGH"),
    (2026,  3, 18, 18, 0, "FOMC Rate Decision",                      "HIGH"),
    (2026,  4, 29, 18, 0, "FOMC Rate Decision",                      "HIGH"),
    (2026,  6, 10, 18, 0, "FOMC Rate Decision",                      "HIGH"),
    (2026,  7, 29, 18, 0, "FOMC Rate Decision",                      "HIGH"),
    (2026,  9, 16, 18, 0, "FOMC Rate Decision",                      "HIGH"),
    (2026, 10, 28, 18, 0, "FOMC Rate Decision",                      "HIGH"),
    (2026, 12,  9, 19, 0, "FOMC Rate Decision",                      "HIGH"),

    # Jackson Hole (late August each year, ~14:00 UTC)
    (2025,  8, 22, 14, 0, "Jackson Hole Symposium",                  "HIGH"),
    (2026,  8, 27, 14, 0, "Jackson Hole Symposium",                  "HIGH"),

    # US CPI (typically 2nd Tuesday each month, 12:30 UTC)
    (2025,  7,  9, 12, 30, "US CPI (June)",                          "HIGH"),
    (2025,  8, 12, 12, 30, "US CPI (July)",                          "HIGH"),
    (2025,  9, 10, 12, 30, "US CPI (August)",                        "HIGH"),
    (2025, 10,  8, 12, 30, "US CPI (September)",                     "HIGH"),
    (2025, 11, 12, 13, 30, "US CPI (October)",                       "HIGH"),
    (2025, 12, 10, 13, 30, "US CPI (November)",                      "HIGH"),
    (2026,  1, 14, 13, 30, "US CPI (December)",                      "HIGH"),
    (2026,  2, 11, 13, 30, "US CPI (January)",                       "HIGH"),
    (2026,  3, 11, 12, 30, "US CPI (February)",                      "HIGH"),
    (2026,  4,  8, 12, 30, "US CPI (March)",                         "HIGH"),
    (2026,  5, 13, 12, 30, "US CPI (April)",                         "HIGH"),
    (2026,  6, 10, 12, 30, "US CPI (May)",                           "HIGH"),
    (2026,  7,  8, 12, 30, "US CPI (June)",                          "HIGH"),

    # US Non-Farm Payrolls (first Friday each month, 12:30 UTC)
    (2025,  7,  4, 12, 30, "US Non-Farm Payrolls (June)",            "HIGH"),
    (2025,  8,  1, 12, 30, "US Non-Farm Payrolls (July)",            "HIGH"),
    (2025,  9,  5, 12, 30, "US Non-Farm Payrolls (August)",          "HIGH"),
    (2025, 10,  3, 12, 30, "US Non-Farm Payrolls (September)",       "HIGH"),
    (2025, 11,  7, 13, 30, "US Non-Farm Payrolls (October)",         "HIGH"),
    (2025, 12,  5, 13, 30, "US Non-Farm Payrolls (November)",        "HIGH"),
    (2026,  1,  9, 13, 30, "US Non-Farm Payrolls (December)",        "HIGH"),
    (2026,  2,  6, 13, 30, "US Non-Farm Payrolls (January)",         "HIGH"),
    (2026,  3,  6, 13, 30, "US Non-Farm Payrolls (February)",        "HIGH"),
    (2026,  4,  3, 12, 30, "US Non-Farm Payrolls (March)",           "HIGH"),
    (2026,  5,  8, 12, 30, "US Non-Farm Payrolls (April)",           "HIGH"),
    (2026,  6,  5, 12, 30, "US Non-Farm Payrolls (May)",             "HIGH"),
    (2026,  7,  9, 12, 30, "US Non-Farm Payrolls (June)",            "HIGH"),

    # US PCE (typically last Friday of the month, 12:30 UTC)
    (2025,  7, 25, 12, 30, "US PCE Price Index (June)",              "HIGH"),
    (2025,  8, 29, 12, 30, "US PCE Price Index (July)",              "HIGH"),
    (2025,  9, 26, 12, 30, "US PCE Price Index (August)",            "HIGH"),
    (2025, 10, 31, 12, 30, "US PCE Price Index (September)",         "HIGH"),
    (2025, 11, 26, 13, 30, "US PCE Price Index (October)",           "HIGH"),
    (2025, 12, 22, 13, 30, "US PCE Price Index (November)",          "HIGH"),
    (2026,  1, 30, 13, 30, "US PCE Price Index (December)",          "HIGH"),
    (2026,  2, 27, 13, 30, "US PCE Price Index (January)",           "HIGH"),
    (2026,  3, 27, 12, 30, "US PCE Price Index (February)",          "HIGH"),
    (2026,  4, 30, 12, 30, "US PCE Price Index (March)",             "HIGH"),
    (2026,  5, 29, 12, 30, "US PCE Price Index (April)",             "HIGH"),
    (2026,  6, 26, 12, 30, "US PCE Price Index (May)",               "HIGH"),

    # US GDP (quarterly, advance estimate)
    (2025,  7, 30, 12, 30, "US GDP Q2 Advance Estimate",             "HIGH"),
    (2025, 10, 29, 12, 30, "US GDP Q3 Advance Estimate",             "HIGH"),
    (2026,  1, 28, 13, 30, "US GDP Q4 2025 Advance Estimate",        "HIGH"),
    (2026,  4, 29, 12, 30, "US GDP Q1 Advance Estimate",             "HIGH"),

    # US ISM Manufacturing (first business day of month, 14:00 UTC)
    (2025,  7,  1, 14,  0, "US ISM Manufacturing PMI",               "MEDIUM"),
    (2025,  8,  1, 14,  0, "US ISM Manufacturing PMI",               "MEDIUM"),
    (2025,  9,  2, 14,  0, "US ISM Manufacturing PMI",               "MEDIUM"),
    (2025, 10,  1, 14,  0, "US ISM Manufacturing PMI",               "MEDIUM"),
    (2026,  2,  2, 15,  0, "US ISM Manufacturing PMI",               "MEDIUM"),
    (2026,  3,  2, 15,  0, "US ISM Manufacturing PMI",               "MEDIUM"),

    # OPEC Meetings (approx)
    (2025, 12,  3, 12,  0, "OPEC+ Ministerial Meeting",              "MEDIUM"),
    (2026,  5, 28, 12,  0, "OPEC+ Ministerial Meeting",              "MEDIUM"),

    # RBI (Indian MPC — every 2 months, ~10:00 IST = 4:30 UTC)
    (2025,  8,  6,  4, 30, "RBI MPC Rate Decision",                  "MEDIUM"),
    (2025, 10,  8,  4, 30, "RBI MPC Rate Decision",                  "MEDIUM"),
    (2025, 12,  5,  4, 30, "RBI MPC Rate Decision",                  "MEDIUM"),
    (2026,  2,  7,  4, 30, "RBI MPC Rate Decision",                  "MEDIUM"),
    (2026,  4,  9,  4, 30, "RBI MPC Rate Decision",                  "MEDIUM"),
    (2026,  6,  5,  4, 30, "RBI MPC Rate Decision",                  "MEDIUM"),

    # ECB (every 6 weeks, 12:15 UTC)
    (2025,  7, 24, 12, 15, "ECB Rate Decision",                      "MEDIUM"),
    (2025,  9, 11, 12, 15, "ECB Rate Decision",                      "MEDIUM"),
    (2025, 10, 30, 12, 15, "ECB Rate Decision",                      "MEDIUM"),
    (2025, 12, 11, 13, 15, "ECB Rate Decision",                      "MEDIUM"),
    (2026,  1, 29, 13, 15, "ECB Rate Decision",                      "MEDIUM"),
    (2026,  3, 19, 12, 15, "ECB Rate Decision",                      "MEDIUM"),
    (2026,  4, 30, 12, 15, "ECB Rate Decision",                      "MEDIUM"),
    (2026,  6, 11, 12, 15, "ECB Rate Decision",                      "MEDIUM"),
]


def _build_static_events():
    """Convert STATIC_EVENTS tuples into the standard dict format."""
    now_ts = time.time()
    events = []
    for item in STATIC_EVENTS:
        yr, mo, dy, hh, mm, title, impact = item
        try:
            dt_utc = datetime(yr, mo, dy, hh, mm, 0, tzinfo=timezone.utc)
            ts     = dt_utc.timestamp()
            dt_ist = dt_utc.astimezone(IST)
            events.append({
                "title":    title,
                "impact":   impact,
                "ts_utc":   ts,
                "ts_epoch": int(ts),
                "date_ist": dt_ist.strftime("%d %b %Y"),
                "time_ist": dt_ist.strftime("%H:%M IST"),
                "in_hours": round((ts - now_ts) / 3600, 1),
                "source":   "schedule",
            })
        except Exception:
            pass
    return events


def _try_finnhub(finnhub_token: str):
    """
    Fetch from Finnhub economic calendar.
    Requires a free Finnhub API key (env: FINNHUB_TOKEN).
    Returns list of event dicts or [].
    """
    if not HAS_REQUESTS or not finnhub_token:
        return []

    today = datetime.now(timezone.utc)
    from_d = today.strftime("%Y-%m-%d")
    to_d   = (today + timedelta(days=30)).strftime("%Y-%m-%d")

    try:
        url = (
            f"https://finnhub.io/api/v1/calendar/economic"
            f"?from={from_d}&to={to_d}&token={finnhub_token}"
        )
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        raw  = data.get("economicCalendar", [])

        events = []
        for e in raw:
            # Filter only high + medium impact USD events relevant to XAU
            country = e.get("country", "").upper()
            impact  = e.get("impact", "").upper()
            if country not in ("US", "IN", "EU", "EMU") and impact != "high":
                continue

            ts_str = e.get("time", "")
            try:
                dt_utc = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except Exception:
                continue

            ts     = dt_utc.timestamp()
            dt_ist = dt_utc.astimezone(IST)
            imp_label = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}.get(
                e.get("impact", "low").lower(), "LOW"
            )

            events.append({
                "title":    e.get("event", "Unknown Event"),
                "impact":   imp_label,
                "ts_utc":   ts,
                "ts_epoch": int(ts),
                "date_ist": dt_ist.strftime("%d %b %Y"),
                "time_ist": dt_ist.strftime("%H:%M IST"),
                "in_hours": round((ts - time.time()) / 3600, 1),
                "source":   "finnhub",
            })

        log.info("[MacroCal] Finnhub returned %d events", len(events))
        return events

    except Exception as exc:
        log.warning("[MacroCal] Finnhub fetch failed: %s", exc)
        return []


def _try_fed_rss():
    """
    Parse the Federal Reserve press release RSS.
    Pulls upcoming FOMC statements + minutes releases.
    """
    if not HAS_FEEDPARSER:
        return []

    FED_RSS = "https://www.federalreserve.gov/feeds/press_all.xml"
    try:
        feed = feedparser.parse(FED_RSS)
        events = []
        now_ts = time.time()

        for e in feed.entries[:20]:
            title = e.get("title", "")
            # Only pull forward-looking items (minutes, statements)
            kw = ("FOMC statement", "FOMC minutes", "Federal Open Market",
                  "Federal Reserve issues FOMC")
            if not any(k.lower() in title.lower() for k in kw):
                continue

            published = e.get("published_parsed") or e.get("updated_parsed")
            if not published:
                continue

            ts = time.mktime(published)
            if ts < now_ts - 86400:          # skip events older than 1 day
                continue

            dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
            dt_ist = dt_utc.astimezone(IST)

            events.append({
                "title":    "FOMC — " + title[:60],
                "impact":   "HIGH",
                "ts_utc":   ts,
                "ts_epoch": int(ts),
                "date_ist": dt_ist.strftime("%d %b %Y"),
                "time_ist": dt_ist.strftime("%H:%M IST"),
                "in_hours": round((ts - now_ts) / 3600, 1),
                "source":   "fed_rss",
            })

        log.info("[MacroCal] Fed RSS returned %d events", len(events))
        return events

    except Exception as exc:
        log.warning("[MacroCal] Fed RSS fetch failed: %s", exc)
        return []


def _merge_and_dedupe(lists):
    """Merge multiple event lists, dedupe by title+date, sort by timestamp."""
    seen   = set()
    merged = []
    for evlist in lists:
        for ev in evlist:
            key = f"{ev['title']}|{ev['date_ist']}"
            if key not in seen:
                seen.add(key)
                merged.append(ev)

    # Sort ascending by timestamp
    merged.sort(key=lambda x: x["ts_utc"])
    return merged


def get_macro_events(finnhub_token: str = "", max_days: int = 30) -> list:
    """
    Main public API.
    Returns upcoming macro events (next max_days days) sorted chronologically.
    Uses in-memory cache (TTL = CACHE_TTL seconds).
    """
    global _cache

    with _lock:
        now = time.time()
        if now - _cache["fetched_at"] < CACHE_TTL:
            return _cached_window(_cache["events"], max_days)

        log.info("[MacroCal] Refreshing macro events cache ...")

        # Layer 1: Finnhub live calendar (requires token)
        finnhub_events = _try_finnhub(finnhub_token)

        # Layer 2: Fed RSS (open, no key)
        fed_events = _try_fed_rss()

        # Layer 3: Static schedule (always works)
        static_events = _build_static_events()

        # Finnhub wins if available, static fills any gaps
        if finnhub_events:
            all_events = _merge_and_dedupe([finnhub_events, fed_events])
        else:
            all_events = _merge_and_dedupe([fed_events, static_events])

        # Attach impact meta (colors) for frontend
        for ev in all_events:
            ev["meta"] = IMPACT_META.get(ev["impact"], IMPACT_META["LOW"])

        _cache = {"events": all_events, "fetched_at": now}
        log.info("[MacroCal] Cache refreshed: %d total events", len(all_events))

        return _cached_window(all_events, max_days)


def _cached_window(events: list, max_days: int) -> list:
    """Filter to upcoming events within max_days from now."""
    now    = time.time()
    cutoff = now + max_days * 86400
    return [
        ev for ev in events
        if ev["ts_utc"] >= now - 3600   # allow 1h grace (event just happened)
        and ev["ts_utc"] <= cutoff
    ]
