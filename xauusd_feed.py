"""
xauusd_feed.py — Twelve Data polling feed for XAU/USD and USDINR.
DXY is sourced from the newsbot /api/prices/ endpoint (real DX-Y.NYB).
USDINR is now fetched from Twelve Data /price endpoint every 60s,
replacing the Upstox NSE futures dependency for rate conversion.

Key rotation: up to 3 Twelve Data API keys (TWELVE_DATA_API_KEY,
TWELVE_DATA_API_KEY_2, TWELVE_DATA_API_KEY_3) are tried in order.
When a key hits the per-minute rate limit it is marked exhausted for 60s,
and the next available key is used automatically.
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

# ── API key rotation ──────────────────────────────────────────────────────────

_key_lock            = threading.Lock()
_key_index           = 0
_key_exhausted_until = [0.0, 0.0, 0.0]

_KEY_NAMES = [
    "twelve_data_api_key",
    "twelve_data_api_key_2",
    "twelve_data_api_key_3",
]
_RATE_LIMIT_COOLDOWN  = 65        # per-minute rate limit
_DAILY_LIMIT_COOLDOWN = 86400     # daily credits exhausted (24h)


def _all_keys() -> list[str]:
    return [k for k in _KEY_NAMES if CONFIG.get(k, "")]


def _get_api_key() -> str:
    global _key_index
    with _key_lock:
        keys = _all_keys()
        if not keys:
            return ""
        now = time.time()
        n   = len(keys)
        for i in range(n):
            idx = (_key_index + i) % n
            if now >= _key_exhausted_until[idx]:
                _key_index = idx
                return CONFIG.get(_KEY_NAMES[idx], "")
        soonest = min(range(n), key=lambda i: _key_exhausted_until[i])
        return CONFIG.get(_KEY_NAMES[soonest], "")


def _mark_key_exhausted(key: str, cooldown: float = _RATE_LIMIT_COOLDOWN):
    global _key_index
    with _key_lock:
        keys = _all_keys()
        for i, name in enumerate(_KEY_NAMES):
            if CONFIG.get(name, "") == key:
                _key_exhausted_until[i] = time.time() + cooldown
                label = "24h (daily limit)" if cooldown > 3600 else f"{int(cooldown)}s"
                log.warning(
                    f"[TwelveData] Key slot {i+1} exhausted — cooling down {label}"
                )
                _key_index = (i + 1) % max(len(keys), 1)
                break


def _is_rate_limit_error(data: dict) -> bool:
    msg = (data.get("message") or "").lower()
    return "run out of api credits" in msg or "rate limit" in msg


def _is_daily_exhaustion(data: dict) -> bool:
    """True when key exhausted ALL daily credits (not just per-minute)."""
    msg = (data.get("message") or "").lower()
    return "run out of api credits for the day" in msg or "daily limit" in msg


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


# ── Twelve Data REST helpers ──────────────────────────────────────────────────

def _td_get(params: dict, timeout: int = 10) -> dict | None:
    """
    Execute a Twelve Data GET /time_series request with automatic key rotation.
    Returns parsed JSON dict or None on failure.
    """
    keys_tried = set()
    n_keys = len(_all_keys())
    if n_keys == 0:
        log.warning("[TwelveData] No API keys configured")
        return None

    for attempt in range(n_keys + 1):
        key = _get_api_key()
        if not key or key in keys_tried:
            break
        keys_tried.add(key)
        try:
            p = {**params, "apikey": key, "format": "JSON"}
            r = requests.get("https://api.twelvedata.com/time_series", params=p, timeout=timeout)
            data = r.json()
            if _is_rate_limit_error(data):
                log.warning(f"[TwelveData] {params.get('symbol','')}: {data.get('message')} — rotating key")
                cooldown = _DAILY_LIMIT_COOLDOWN if _is_daily_exhaustion(data) else _RATE_LIMIT_COOLDOWN
                _mark_key_exhausted(key, cooldown)
                continue
            if data.get("status") == "error":
                log.warning(f"[TwelveData] {params.get('symbol','')}: {data.get('message')}")
                return None
            return data
        except Exception as e:
            log.warning(f"[TwelveData] request error: {e}")
            return None

    log.warning("[TwelveData] All keys exhausted or failed — skipping fetch")
    return None


def _td_price(symbol: str, timeout: int = 8) -> float:
    """
    Fetch a single latest price from Twelve Data /price endpoint.
    Uses only 1 API credit. Returns float or 0.0 on failure.
    Uses the same key rotation as _td_get.
    """
    keys_tried = set()
    n_keys = len(_all_keys())
    if n_keys == 0:
        return 0.0

    for attempt in range(n_keys + 1):
        key = _get_api_key()
        if not key or key in keys_tried:
            break
        keys_tried.add(key)
        try:
            r = requests.get(
                "https://api.twelvedata.com/price",
                params={"symbol": symbol, "apikey": key},
                timeout=timeout,
            )
            data = r.json()
            if _is_rate_limit_error(data):
                log.warning(f"[TwelveData] /price {symbol}: rate limit — rotating key")
                cooldown = _DAILY_LIMIT_COOLDOWN if _is_daily_exhaustion(data) else _RATE_LIMIT_COOLDOWN
                _mark_key_exhausted(key, cooldown)
                continue
            price = float(data.get("price", 0) or 0)
            if price > 0:
                return price
            log.warning(f"[TwelveData] /price {symbol}: unexpected response {data}")
            return 0.0
        except Exception as e:
            log.warning(f"[TwelveData] /price {symbol} error: {e}")
            return 0.0

    return 0.0


# ── USDINR via Twelve Data ────────────────────────────────────────────────────

def fetch_usdinr_twelvedata() -> float:
    """
    Fetch live USDINR spot rate from Twelve Data /price endpoint.
    Caches result in CONFIG["usdinr_live"] and returns the float.
    Falls back to last cached value if fetch fails.
    """
    price = _td_price("USD/INR")
    if price > 0:
        CONFIG["usdinr_live"] = price
        CONFIG["usdinr_source"] = "twelvedata"
        log.debug(f"[TwelveData] USD/INR = {price:.4f}")
        return price
    # Return last cached value so conversion doesn't break on a single miss
    cached = float(CONFIG.get("usdinr_live", 0) or 0)
    if cached > 0:
        log.debug(f"[TwelveData] USD/INR fetch failed — using cached {cached:.4f}")
    else:
        log.warning("[TwelveData] USD/INR fetch failed and no cached value available")
    return cached


def get_usdinr_live() -> float:
    """
    Return current USDINR rate.
    Respects the evening freeze: after 17:00 IST uses frozen rate.
    Falls back to Twelve Data spot if no frozen/cached value.
    """
    if CONFIG.get("usdinr_is_frozen"):
        return float(CONFIG.get("usdinr_frozen", 0) or 0)
    rate = float(CONFIG.get("usdinr_live", 0) or 0)
    if rate > 0:
        return rate
    # Nothing cached yet — fetch immediately
    return fetch_usdinr_twelvedata()


# ── Candle fetch helpers ──────────────────────────────────────────────────────

def _fetch_latest_candle(twelve_symbol: str) -> dict | None:
    """Fetch the most recent completed 1m candle from Twelve Data."""
    data = _td_get({"symbol": twelve_symbol, "interval": "1min", "outputsize": 2})
    if data is None:
        return None
    values = data.get("values", [])
    if len(values) < 2:
        return None
    raw = values[1]   # values[0] = forming, values[1] = last closed
    ts = datetime.strptime(raw["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return {
        "timestamp": ts,
        "open":  float(raw["open"]),
        "high":  float(raw["high"]),
        "low":   float(raw["low"]),
        "close": float(raw["close"]),
    }


def _fetch_historical(twelve_symbol: str, outputsize: int = 100) -> list:
    """Seed the buffer with historical 1m candles on startup."""
    data = _td_get({"symbol": twelve_symbol, "interval": "1min", "outputsize": outputsize}, timeout=15)
    if data is None:
        return []
    candles = []
    for raw in reversed(data.get("values", [])[1:]):
        ts = datetime.strptime(raw["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        candles.append({
            "timestamp": ts,
            "open":  float(raw["open"]),
            "high":  float(raw["high"]),
            "low":   float(raw["low"]),
            "close": float(raw["close"]),
        })
    return candles


# ── Seed buffers on startup ───────────────────────────────────────────────────

def seed_buffers():
    """Call once at startup to populate candle buffers and USDINR."""
    log.info("[TwelveData] Seeding XAU/USD buffer...")
    xau_candles = _fetch_historical(_xauusd.twelve_symbol, outputsize=100)
    for c in xau_candles:
        _xauusd.push_1m_candle(c)
    log.info(f"[TwelveData] XAU/USD seeded with {len(xau_candles)} 1m candles")

    log.info("[TwelveData] DXY polling disabled — using newsbot real DXY (DX-Y.NYB)")

    _seed_higher_tf(_xauusd, "5min",  60)
    _seed_higher_tf(_xauusd, "15min", 50)

    # Seed USDINR immediately so conversion is ready before first tick
    log.info("[TwelveData] Fetching initial USDINR from Twelve Data...")
    rate = fetch_usdinr_twelvedata()
    if rate > 0:
        log.info(f"[TwelveData] Initial USD/INR = {rate:.4f}")
    else:
        log.warning("[TwelveData] Initial USD/INR fetch failed — will retry in poll loop")


def _seed_higher_tf(feed: SymbolFeed, interval: str, size: int):
    """Directly fetch 5m/15m candles to pre-populate aggregated buffers."""
    data = _td_get({"symbol": feed.twelve_symbol, "interval": interval, "outputsize": size}, timeout=15)
    if data is None:
        return
    buf = feed.buf_5m if interval == "5min" else feed.buf_15m
    for raw in reversed(data.get("values", [])[1:]):
        ts = datetime.strptime(raw["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        c = {
            "timestamp": ts,
            "open":  float(raw["open"]),
            "high":  float(raw["high"]),
            "low":   float(raw["low"]),
            "close": float(raw["close"]),
        }
        buf.candles.append(c)
    log.info(f"[TwelveData] {feed.symbol} {interval} seeded with {len(buf.candles)} candles")


# ── Polling loop ──────────────────────────────────────────────────────────────

_last_xau_ts    = None
_usdinr_counter = 0          # poll USDINR every N XAU ticks (saves credits)
_USDINR_EVERY   = 1          # fetch USDINR on every tick (60s) — costs 1 credit/min


def _poll_loop():
    global _last_xau_ts, _usdinr_counter
    interval = CONFIG.get("xauusd_poll_interval", 60)
    log.info(f"[TwelveData] Poll loop started — XAU/USD + USD/INR every {interval}s")

    while True:
        try:
            # ── XAU/USD candle ────────────────────────────────────────────────
            c = _fetch_latest_candle(_xauusd.twelve_symbol)
            if c and c["timestamp"] != _last_xau_ts:
                _last_xau_ts = c["timestamp"]
                with _lock:
                    _xauusd.push_1m_candle(c)
                CONFIG["xauusd_last"] = c["close"]
                log.debug(f"[TwelveData] XAU/USD 1m close={c['close']:.3f}")

            # ── USDINR price ──────────────────────────────────────────────────
            # Only fetch when market not frozen (NSE currency closes 17:00 IST)
            now_ist = datetime.now(IST)
            market_open = 9 <= now_ist.hour < 17

            if market_open:
                _usdinr_counter += 1
                if _usdinr_counter >= _USDINR_EVERY:
                    _usdinr_counter = 0
                    rate = fetch_usdinr_twelvedata()
                    if rate > 0:
                        # Evening freeze logic: freeze at 17:00 IST
                        pass   # freeze handled separately in upstox_client / app.py
            else:
                # After 17:00 — freeze if not already frozen
                if not CONFIG.get("usdinr_is_frozen") and CONFIG.get("usdinr_live", 0):
                    CONFIG["usdinr_frozen"]    = CONFIG["usdinr_live"]
                    CONFIG["usdinr_is_frozen"] = True
                    log.info(
                        f"[TwelveData] USDINR frozen at {CONFIG['usdinr_frozen']:.4f} "
                        f"(NSE currency closed)"
                    )

        except Exception as e:
            log.warning(f"[TwelveData] Poll error: {e}")

        time.sleep(interval)


def start_polling():
    """Start background polling thread for XAU/USD and USD/INR."""
    t = threading.Thread(target=_poll_loop, daemon=True, name="XAUUSDPoller")
    t.start()
    log.info("[TwelveData] Polling thread started (XAU/USD + USD/INR)")
    return t