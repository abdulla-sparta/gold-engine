"""
xauusd_feed.py — Twelve Data polling feed for XAU/USD only.
DXY is now sourced from the newsbot /api/prices/ endpoint (real DX-Y.NYB, not EUR/USD proxy).
This saves ~400 Twelve Data credits/day.
Builds 1m, 5m, 15m OHLC candle buffers internally.
"""
import time
import threading
import logging
import requests
from datetime import datetime, timezone, timedelta
from collections import deque
from config import CONFIG

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Candle buffer ─────────────────────────────────────────────────────────────

class CandleBuffer:
    """Aggregates 1m ticks into N-minute OHLC candles."""

    def __init__(self, timeframe_minutes: int, maxlen: int = 200):
        self.tf_min   = timeframe_minutes
        self.candles  = deque(maxlen=maxlen)
        self._current = None   # candle being built

    def push_1m(self, candle_1m: dict):
        """Accept a 1m OHLC dict and aggregate into this buffer's timeframe."""
        ts = candle_1m["timestamp"]  # datetime
        # Which N-min bucket does this 1m belong to?
        bucket_min = (ts.minute // self.tf_min) * self.tf_min
        bucket_ts  = ts.replace(minute=bucket_min, second=0, microsecond=0)

        if self._current is None or self._current["timestamp"] != bucket_ts:
            if self._current is not None:
                self.candles.append(self._current)
            self._current = {
                "timestamp": bucket_ts,
                "open":  candle_1m["open"],
                "high":  candle_1m["high"],
                "low":   candle_1m["low"],
                "close": candle_1m["close"],
            }
        else:
            c = self._current
            c["high"]  = max(c["high"],  candle_1m["high"])
            c["low"]   = min(c["low"],   candle_1m["low"])
            c["close"] = candle_1m["close"]

    def latest(self, n: int = 1):
        """Return latest N closed candles (excludes current forming candle)."""
        lst = list(self.candles)
        return lst[-n:] if n > 1 else (lst[-1] if lst else None)

    def all_closed(self):
        return list(self.candles)


# ── Symbol state ──────────────────────────────────────────────────────────────

class SymbolFeed:
    def __init__(self, symbol: str, twelve_symbol: str):
        self.symbol        = symbol
        self.twelve_symbol = twelve_symbol
        self.buf_1m        = deque(maxlen=500)
        self.buf_5m        = CandleBuffer(5,  maxlen=200)
        self.buf_15m       = CandleBuffer(15, maxlen=100)
        self.last_price    = 0.0
        self.last_update   = None

    def push_1m_candle(self, c: dict):
        self.buf_1m.append(c)
        self.buf_5m.push_1m(c)
        self.buf_15m.push_1m(c)
        self.last_price  = c["close"]
        self.last_update = c["timestamp"]


# ── Singleton feeds ───────────────────────────────────────────────────────────

_xauusd = SymbolFeed("XAU/USD", "XAU/USD")
_dxy    = SymbolFeed("DXY",     "EUR/USD")
_lock   = threading.Lock()


def get_xauusd() -> SymbolFeed:
    return _xauusd

def get_dxy() -> SymbolFeed:
    return _dxy


# ── Twelve Data REST fetch ────────────────────────────────────────────────────

def _fetch_latest_candle(twelve_symbol: str) -> dict | None:
    """Fetch the most recent completed 1m candle from Twelve Data."""
    api_key = CONFIG.get("twelve_data_api_key", "")
    if not api_key:
        log.warning("[TwelveData] API key not set")
        return None
    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol":     twelve_symbol,
            "interval":   "1min",
            "outputsize": 2,        # get 2 so we always have 1 closed candle
            "apikey":     api_key,
            "format":     "JSON",
        }
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("status") == "error":
            log.warning(f"[TwelveData] {twelve_symbol}: {data.get('message')}")
            return None
        values = data.get("values", [])
        if len(values) < 2:
            return None
        # values[0] = newest (possibly still forming), values[1] = last closed
        raw = values[1]
        ts_str = raw["datetime"]   # "2026-06-06 14:32:00"
        # Twelve Data returns in exchange timezone — XAU/USD is UTC
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return {
            "timestamp": ts,
            "open":  float(raw["open"]),
            "high":  float(raw["high"]),
            "low":   float(raw["low"]),
            "close": float(raw["close"]),
        }
    except Exception as e:
        log.warning(f"[TwelveData] fetch error for {twelve_symbol}: {e}")
        return None


def _fetch_historical(twelve_symbol: str, outputsize: int = 100) -> list:
    """Seed the buffer with historical 1m candles on startup."""
    api_key = CONFIG.get("twelve_data_api_key", "")
    if not api_key:
        return []
    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol":     twelve_symbol,
            "interval":   "1min",
            "outputsize": outputsize,
            "apikey":     api_key,
            "format":     "JSON",
        }
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("status") == "error":
            log.warning(f"[TwelveData] historical {twelve_symbol}: {data.get('message')}")
            return []
        values = data.get("values", [])
        candles = []
        for raw in reversed(values[1:]):   # oldest first, skip newest (forming)
            ts = datetime.strptime(raw["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            candles.append({
                "timestamp": ts,
                "open":  float(raw["open"]),
                "high":  float(raw["high"]),
                "low":   float(raw["low"]),
                "close": float(raw["close"]),
            })
        return candles
    except Exception as e:
        log.warning(f"[TwelveData] historical fetch error: {e}")
        return []


# ── Seed buffers on startup ───────────────────────────────────────────────────

def seed_buffers():
    """Call once at startup to populate 15m buffer (needs ~100 1m candles)."""
    log.info("[TwelveData] Seeding XAU/USD buffer...")
    xau_candles = _fetch_historical(_xauusd.twelve_symbol, outputsize=100)
    for c in xau_candles:
        _xauusd.push_1m_candle(c)
    log.info(f"[TwelveData] XAU/USD seeded with {len(xau_candles)} 1m candles")

    # DXY is now sourced from newsbot /api/prices/ — no Twelve Data credits used
    log.info("[TwelveData] DXY polling disabled — using newsbot real DXY (DX-Y.NYB)")

    # Seed 5m/15m from historical 5m endpoint too
    _seed_higher_tf(_xauusd, "5min", 60)
    _seed_higher_tf(_xauusd, "15min", 50)


def _seed_higher_tf(feed: SymbolFeed, interval: str, size: int):
    """Directly fetch 5m/15m candles to pre-populate aggregated buffers."""
    api_key = CONFIG.get("twelve_data_api_key", "")
    if not api_key:
        return
    try:
        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": feed.twelve_symbol, "interval": interval,
            "outputsize": size, "apikey": api_key, "format": "JSON"
        }, timeout=15)
        data = r.json()
        if data.get("status") == "error":
            return
        buf = feed.buf_5m if interval == "5min" else feed.buf_15m
        for raw in reversed(data.get("values", [])[1:]):
            ts = datetime.strptime(raw["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            c = {"timestamp": ts, "open": float(raw["open"]), "high": float(raw["high"]),
                 "low": float(raw["low"]), "close": float(raw["close"])}
            buf.candles.append(c)
        log.info(f"[TwelveData] {feed.symbol} {interval} seeded with {len(buf.candles)} candles")
    except Exception as e:
        log.warning(f"[TwelveData] seed_higher_tf error: {e}")


# ── Polling thread ────────────────────────────────────────────────────────────

_last_xau_ts = None
_last_dxy_ts = None


def _poll_loop():
    global _last_xau_ts, _last_dxy_ts
    interval = CONFIG.get("xauusd_poll_interval", 60)
    log.info(f"[TwelveData] Poll loop started — interval={interval}s")

    while True:
        try:
            # XAU/USD
            c = _fetch_latest_candle(_xauusd.twelve_symbol)
            if c and c["timestamp"] != _last_xau_ts:
                _last_xau_ts = c["timestamp"]
                with _lock:
                    _xauusd.push_1m_candle(c)
                CONFIG["xauusd_last"] = c["close"]
                log.debug(f"[TwelveData] XAU/USD 1m close={c['close']:.3f}")

            # DXY now comes from newsbot intelligence_client — no polling here

        except Exception as e:
            log.warning(f"[TwelveData] Poll error: {e}")

        time.sleep(interval)


def start_polling():
    """Start background polling thread."""
    t = threading.Thread(target=_poll_loop, daemon=True, name="XAUUSDPoller")
    t.start()
    log.info("[TwelveData] Polling thread started")
    return t
