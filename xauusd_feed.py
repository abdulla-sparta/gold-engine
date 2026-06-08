"""
xauusd_feed.py — yfinance-based feed for XAU/USD and DXY (EUR/USD proxy)

Replaces Twelve Data (which exhausted its 800 free credits/day).
yfinance uses Yahoo Finance — no API key, no rate limits, no credits.

Symbols used:
  XAU/USD  →  XAUUSD=X   (spot gold in USD)
  EUR/USD  →  EURUSD=X   (DXY proxy — inverted, EUR = 57.6% of DXY basket)

Polling strategy (free, unlimited):
  - Startup seed: 1d history at 1m interval  (~390 candles)
  - Live poll: every 60s, fetch last 2 candles, push the closed one
  - 5m / 15m buffers are aggregated internally from 1m pushes
  - Higher-TF seed: also fetches 5m/15m directly on startup for richer history
"""

import time
import threading
import logging
from datetime import datetime, timezone, timedelta
from collections import deque

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

from config import CONFIG

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Yahoo Finance symbol map
_YF_SYMBOLS = {
    "XAU/USD": "GC=F",
    "EUR/USD": "EURUSD=X",
}

# ── Candle buffer ─────────────────────────────────────────────────────────────

class CandleBuffer:
    """Aggregates 1m ticks into N-minute OHLC candles."""

    def __init__(self, timeframe_minutes: int, maxlen: int = 200):
        self.tf_min   = timeframe_minutes
        self.candles  = deque(maxlen=maxlen)
        self._current = None

    def push_1m(self, candle_1m: dict):
        ts = candle_1m["timestamp"]
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
        lst = list(self.candles)
        return lst[-n:] if n > 1 else (lst[-1] if lst else None)

    def all_closed(self):
        return list(self.candles)


# ── Symbol state ──────────────────────────────────────────────────────────────

class SymbolFeed:
    def __init__(self, symbol: str, yf_symbol: str):
        self.symbol     = symbol
        self.yf_symbol  = yf_symbol
        self.buf_1m     = deque(maxlen=500)
        self.buf_5m     = CandleBuffer(5,  maxlen=200)
        self.buf_15m    = CandleBuffer(15, maxlen=100)
        self.last_price = 0.0
        self.last_update = None

    def push_1m_candle(self, c: dict):
        self.buf_1m.append(c)
        self.buf_5m.push_1m(c)
        self.buf_15m.push_1m(c)
        self.last_price  = c["close"]
        self.last_update = c["timestamp"]


# ── Singleton feeds ───────────────────────────────────────────────────────────

_xauusd = SymbolFeed("XAU/USD", _YF_SYMBOLS["XAU/USD"])
_dxy    = SymbolFeed("DXY",     _YF_SYMBOLS["EUR/USD"])
_lock   = threading.Lock()


def get_xauusd() -> SymbolFeed:
    return _xauusd

def get_dxy() -> SymbolFeed:
    return _dxy


# ── yfinance fetch helpers ────────────────────────────────────────────────────

def _yf_to_candle(row, idx) -> dict:
    """Convert a yfinance DataFrame row to our candle dict (UTC-aware)."""
    ts = idx
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.to_pydatetime().astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    return {
        "timestamp": ts,
        "open":  float(row["Open"]),
        "high":  float(row["High"]),
        "low":   float(row["Low"]),
        "close": float(row["Close"]),
    }


def _fetch_historical_1m(yf_symbol: str, outputsize: int = 200) -> list:
    """Seed buffer with recent 1m candles from yfinance (last trading day)."""
    if not _YF_AVAILABLE:
        log.warning("[yfinance] yfinance not installed — pip install yfinance")
        return []
    try:
        tk   = yf.Ticker(yf_symbol)
        hist = tk.history(period="1d", interval="1m", auto_adjust=True)
        if hist.empty:
            log.warning(f"[yfinance] {yf_symbol} returned empty history")
            return []
        # Drop the last row (still-forming candle), keep the rest
        hist = hist.iloc[:-1]
        # Take the most recent `outputsize` rows
        hist = hist.tail(outputsize)
        candles = [_yf_to_candle(row, idx) for idx, row in hist.iterrows()]
        return candles
    except Exception as e:
        log.warning(f"[yfinance] historical 1m fetch error for {yf_symbol}: {e}")
        return []


def _fetch_historical_tf(yf_symbol: str, interval: str, outputsize: int) -> list:
    """Fetch 5m or 15m candles directly for richer startup seeding."""
    if not _YF_AVAILABLE:
        return []
    try:
        # yfinance interval map: "5m", "15m"  (yf uses "5m" not "5min")
        yf_interval = interval.replace("min", "m")
        period = "5d"   # enough for 200 candles of any TF
        tk   = yf.Ticker(yf_symbol)
        hist = tk.history(period=period, interval=yf_interval, auto_adjust=True)
        if hist.empty:
            return []
        hist = hist.iloc[:-1].tail(outputsize)   # drop forming candle
        return [_yf_to_candle(row, idx) for idx, row in hist.iterrows()]
    except Exception as e:
        log.warning(f"[yfinance] historical {interval} fetch error for {yf_symbol}: {e}")
        return []


def _fetch_latest_1m_candle(yf_symbol: str) -> dict | None:
    """Fetch the most recent completed 1m candle (live poll)."""
    if not _YF_AVAILABLE:
        return None
    try:
        tk   = yf.Ticker(yf_symbol)
        hist = tk.history(period="1d", interval="1m", auto_adjust=True)
        if hist.empty or len(hist) < 2:
            return None
        # index[-2] = last fully-closed 1m bar  (index[-1] is still forming)
        return _yf_to_candle(hist.iloc[-2], hist.index[-2])
    except Exception as e:
        log.warning(f"[yfinance] live fetch error for {yf_symbol}: {e}")
        return None


# ── Seed buffers on startup ───────────────────────────────────────────────────

def seed_buffers():
    """Call once at startup to populate candle buffers."""
    log.info("[yfinance] Seeding XAU/USD buffer...")
    xau_candles = _fetch_historical_1m(_xauusd.yf_symbol, outputsize=200)
    for c in xau_candles:
        _xauusd.push_1m_candle(c)
    log.info(f"[yfinance] XAU/USD seeded with {len(xau_candles)} 1m candles")

    if CONFIG.get("dxy_enabled"):
        log.info("[yfinance] Seeding DXY (EUR/USD) buffer...")
        dxy_candles = _fetch_historical_1m(_dxy.yf_symbol, outputsize=200)
        for c in dxy_candles:
            _dxy.push_1m_candle(c)
        log.info(f"[yfinance] DXY seeded with {len(dxy_candles)} 1m candles")

    # Seed higher timeframes directly for richer structure analysis
    _seed_higher_tf(_xauusd, "5m",  60)
    _seed_higher_tf(_xauusd, "15m", 50)


def _seed_higher_tf(feed: SymbolFeed, interval: str, size: int):
    """Directly seed 5m/15m CandleBuffer from yfinance higher-TF data."""
    candles = _fetch_historical_tf(feed.yf_symbol, interval, size)
    if not candles:
        return
    buf = feed.buf_5m if interval == "5m" else feed.buf_15m
    for c in candles:
        buf.candles.append(c)
    log.info(f"[yfinance] {feed.symbol} {interval} seeded with {len(candles)} candles")


# ── Polling thread ────────────────────────────────────────────────────────────

_last_xau_ts = None
_last_dxy_ts = None


def _poll_loop():
    global _last_xau_ts, _last_dxy_ts
    interval = CONFIG.get("xauusd_poll_interval", 60)
    log.info(f"[yfinance] Poll loop started — interval={interval}s")

    dxy_counter = 0   # poll DXY every 2nd cycle to reduce load

    while True:
        try:
            # ── XAU/USD ───────────────────────────────────────────────────────
            c = _fetch_latest_1m_candle(_xauusd.yf_symbol)
            if c and c["timestamp"] != _last_xau_ts:
                _last_xau_ts = c["timestamp"]
                with _lock:
                    _xauusd.push_1m_candle(c)
                CONFIG["xauusd_last"] = c["close"]
                log.debug(f"[yfinance] XAU/USD 1m close={c['close']:.3f}")

            # ── DXY (every 2nd poll) ──────────────────────────────────────────
            dxy_counter += 1
            if CONFIG.get("dxy_enabled") and dxy_counter % 2 == 0:
                c_dxy = _fetch_latest_1m_candle(_dxy.yf_symbol)
                if c_dxy and c_dxy["timestamp"] != _last_dxy_ts:
                    _last_dxy_ts = c_dxy["timestamp"]
                    with _lock:
                        _dxy.push_1m_candle(c_dxy)
                    CONFIG["dxy_last"] = c_dxy["close"]
                    log.debug(f"[yfinance] EUR/USD 1m close={c_dxy['close']:.5f}")

        except Exception as e:
            log.warning(f"[yfinance] Poll error: {e}")

        time.sleep(interval)


def start_polling():
    """Start background polling thread."""
    t = threading.Thread(target=_poll_loop, daemon=True, name="XAUUSDPoller")
    t.start()
    log.info("[yfinance] Polling thread started")
    return t
