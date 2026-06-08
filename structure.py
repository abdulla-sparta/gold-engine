"""
structure.py — Market structure analysis on XAU/USD candles
  - HTF 15m: BOS pivot bias detection (BULLISH / BEARISH / NONE)
  - LTF 5m:  BOS/CHoCH entry signal
  - SwingLevelTracker: stores confirmed swings, checks MCX proximity
"""
import logging
from datetime import datetime, timezone, timedelta
from config import CONFIG
import db

log = logging.getLogger(__name__)


# ── Pivot detection ───────────────────────────────────────────────────────────

def find_pivot_high(candles: list, idx: int, left: int, right: int) -> bool:
    """True if candles[idx] is a confirmed pivot high."""
    if idx < left or idx + right >= len(candles):
        return False
    pivot = candles[idx]["high"]
    for i in range(idx - left, idx):
        if candles[i]["high"] >= pivot:
            return False
    for i in range(idx + 1, idx + right + 1):
        if candles[i]["high"] >= pivot:
            return False
    return True


def find_pivot_low(candles: list, idx: int, left: int, right: int) -> bool:
    """True if candles[idx] is a confirmed pivot low."""
    if idx < left or idx + right >= len(candles):
        return False
    pivot = candles[idx]["low"]
    for i in range(idx - left, idx):
        if candles[i]["low"] <= pivot:
            return False
    for i in range(idx + 1, idx + right + 1):
        if candles[i]["low"] <= pivot:
            return False
    return True


# ── HTF Bias Engine ───────────────────────────────────────────────────────────

class HTFBiasEngine:
    """
    Tracks BOS on 15m XAU/USD candles.
    State: BULLISH / BEARISH / NONE
    Pivot is consumed after each BOS — prevents stale ATH/ATL reference.
    """

    def __init__(self):
        self.bias          = "NONE"
        self.ref_high      = None   # last confirmed swing high price
        self.ref_low       = None   # last confirmed swing low price
        self.ref_high_ts   = None
        self.ref_low_ts    = None
        self.last_bos_ts   = None

    def update(self, candles: list) -> str:
        """Feed latest 15m candle list. Returns current bias."""
        if len(candles) < 10:
            return self.bias

        left  = CONFIG.get("htf_pivot_left",  3)
        right = CONFIG.get("htf_pivot_right", 3)
        n     = len(candles)

        # Scan for new pivot highs/lows in the last (right+1) completed candles
        for idx in range(max(0, n - right - 5), n - right):
            if find_pivot_high(candles, idx, left, right):
                ph = candles[idx]["high"]
                ts = candles[idx]["timestamp"]
                if self.ref_high is None or ph > self.ref_high:
                    self.ref_high    = ph
                    self.ref_high_ts = ts
                    log.debug(f"[HTF] New pivot HIGH: {ph:.3f} @ {ts}")

            if find_pivot_low(candles, idx, left, right):
                pl = candles[idx]["low"]
                ts = candles[idx]["timestamp"]
                if self.ref_low is None or pl < self.ref_low:
                    self.ref_low    = pl
                    self.ref_low_ts = ts
                    log.debug(f"[HTF] New pivot LOW: {pl:.3f} @ {ts}")

        # BOS check on latest close
        last_close = candles[-1]["close"]
        last_ts    = candles[-1]["timestamp"]

        if self.ref_high and last_close > self.ref_high:
            if self.last_bos_ts != last_ts:
                log.info(f"[HTF] BOS BULLISH — close {last_close:.3f} > pivot high {self.ref_high:.3f}")
                self.bias        = "BULLISH"
                self.last_bos_ts = last_ts
                self.ref_high    = None   # consume pivot

        elif self.ref_low and last_close < self.ref_low:
            if self.last_bos_ts != last_ts:
                log.info(f"[HTF] BOS BEARISH — close {last_close:.3f} < pivot low {self.ref_low:.3f}")
                self.bias        = "BEARISH"
                self.last_bos_ts = last_ts
                self.ref_low     = None   # consume pivot

        return self.bias

    def status(self) -> dict:
        return {
            "bias":        self.bias,
            "ref_high":    self.ref_high,
            "ref_low":     self.ref_low,
            "last_bos_ts": str(self.last_bos_ts) if self.last_bos_ts else None,
        }


# ── LTF Entry Engine ──────────────────────────────────────────────────────────

class LTFEntryEngine:
    """
    Detects BOS/CHoCH on 5m XAU/USD candles.
    Only fires signals in the direction of HTF bias.
    """

    def __init__(self):
        self.last_signal_ts = None
        self.ref_high       = None
        self.ref_low        = None

    def check(self, candles: list, htf_bias: str) -> dict | None:
        """
        Returns signal dict if entry condition met, else None.
        Signal: {"direction": "BUY"/"SELL", "price": float, "candle_ts": datetime}
        """
        if len(candles) < 8 or htf_bias == "NONE":
            return None

        left  = CONFIG.get("ltf_pivot_left",  2)
        right = CONFIG.get("ltf_pivot_right", 2)
        n     = len(candles)

        # Update LTF pivots
        for idx in range(max(0, n - right - 3), n - right):
            if find_pivot_high(candles, idx, left, right):
                ph = candles[idx]["high"]
                if self.ref_high is None or ph != self.ref_high:
                    self.ref_high = ph
            if find_pivot_low(candles, idx, left, right):
                pl = candles[idx]["low"]
                if self.ref_low is None or pl != self.ref_low:
                    self.ref_low = pl

        last      = candles[-1]
        last_close = last["close"]
        last_ts    = last["timestamp"]

        if last_ts == self.last_signal_ts:
            return None   # already fired on this candle

        # BUY signal: HTF bullish + LTF BOS above swing high
        if htf_bias == "BULLISH" and self.ref_high and last_close > self.ref_high:
            self.last_signal_ts = last_ts
            self.ref_high       = None   # consume
            log.info(f"[LTF] BUY signal @ {last_close:.3f}")
            return {
                "direction":  "BUY",
                "xau_price":  last_close,
                "xau_stop":   self.ref_low or (last_close * 0.995),
                "candle_ts":  last_ts,
            }

        # SELL signal: HTF bearish + LTF BOS below swing low
        if htf_bias == "BEARISH" and self.ref_low and last_close < self.ref_low:
            self.last_signal_ts = last_ts
            self.ref_low        = None   # consume
            log.info(f"[LTF] SELL signal @ {last_close:.3f}")
            return {
                "direction":  "SELL",
                "xau_price":  last_close,
                "xau_stop":   self.ref_high or (last_close * 1.005),
                "candle_ts":  last_ts,
            }

        return None


# ── Swing Level Tracker ───────────────────────────────────────────────────────

class SwingLevelTracker:
    """
    Your core edge:
    1. Detects confirmed swing H/L on 15m XAU/USD
    2. Converts each to MCX equivalent at detection time
    3. Checks if current GOLDTEN price is within threshold% of that level
    4. Returns matching levels for confluence check
    """

    def __init__(self):
        self._levels = []   # in-memory cache (also persisted to DB)
        self._last_scan_ts = None

    def load_from_db(self):
        """Restore swing levels from DB on startup."""
        max_age = CONFIG.get("swing_max_age_hours", 48)
        self._levels = list(db.get_active_swings(max_age))
        log.info(f"[SwingTracker] Loaded {len(self._levels)} active swing levels from DB")

    def scan(self, candles_15m: list, usdinr: float):
        """
        Scan 15m XAU/USD candles for new confirmed pivot H/L.
        Converts and stores each new swing.
        """
        if len(candles_15m) < 10:
            return

        left  = CONFIG.get("htf_pivot_left",  3)
        right = CONFIG.get("htf_pivot_right", 3)
        n     = len(candles_15m)
        oz    = CONFIG.get("oz_to_10gms", 0.35274)
        max_n = CONFIG.get("swing_max_levels", 10)

        for idx in range(max(0, n - right - 5), n - right):
            c   = candles_15m[idx]
            ts  = c["timestamp"]

            if ts == self._last_scan_ts:
                continue

            # Pivot HIGH
            if find_pivot_high(candles_15m, idx, left, right):
                xau_p    = c["high"]
                mcx_eq   = round(xau_p * oz * usdinr, 0)
                existing = [s for s in self._levels
                            if abs(float(s.get("xau_price", 0)) - xau_p) < 0.5]
                if not existing:
                    swing = {
                        "xau_price":   xau_p,
                        "mcx_equiv":   mcx_eq,
                        "usdinr_rate": usdinr,
                        "swing_type":  "swing_high",
                        "timestamp":   ts,
                        "touched":     False,
                    }
                    swing_id = db.save_swing(swing)
                    swing["id"] = swing_id
                    self._levels.append(swing)
                    log.info(f"[SwingTracker] NEW swing HIGH: XAU={xau_p:.3f} → MCX≈₹{mcx_eq:,.0f}")
                    self._last_scan_ts = ts

            # Pivot LOW
            if find_pivot_low(candles_15m, idx, left, right):
                xau_p    = c["low"]
                mcx_eq   = round(xau_p * oz * usdinr, 0)
                existing = [s for s in self._levels
                            if abs(float(s.get("xau_price", 0)) - xau_p) < 0.5]
                if not existing:
                    swing = {
                        "xau_price":   xau_p,
                        "mcx_equiv":   mcx_eq,
                        "usdinr_rate": usdinr,
                        "swing_type":  "swing_low",
                        "timestamp":   ts,
                        "touched":     False,
                    }
                    swing_id = db.save_swing(swing)
                    swing["id"] = swing_id
                    self._levels.append(swing)
                    log.info(f"[SwingTracker] NEW swing LOW: XAU={xau_p:.3f} → MCX≈₹{mcx_eq:,.0f}")
                    self._last_scan_ts = ts

        # Trim to max_n levels (keep most recent)
        if len(self._levels) > max_n:
            self._levels = self._levels[-max_n:]

    def nearby_levels(self, goldten_price: float) -> list:
        """
        Return swing levels where GOLDTEN is within threshold% of mcx_equiv.
        """
        threshold = CONFIG.get("swing_level_threshold_pct", 0.30) / 100.0
        nearby    = []
        for lvl in self._levels:
            if lvl.get("touched"):
                continue
            mcx_eq = float(lvl.get("mcx_equiv", 0))
            if mcx_eq <= 0:
                continue
            dist_pct = abs(goldten_price - mcx_eq) / mcx_eq * 100
            if dist_pct <= CONFIG.get("swing_level_threshold_pct", 0.30):
                nearby.append({
                    **dict(lvl),
                    "dist_pct":      round(dist_pct, 3),
                    "goldten_price": goldten_price,
                })
        return nearby

    def mark_touched(self, swing_id: int):
        db.mark_swing_touched(swing_id)
        for lvl in self._levels:
            if lvl.get("id") == swing_id:
                lvl["touched"] = True

    def all_levels(self) -> list:
        return list(self._levels)


# ── DXY Confluence Filter ─────────────────────────────────────────────────────

def dxy_confluence(dxy_candles_15m: list, signal_direction: str) -> bool:
    """
    DXY direction derived from EUR/USD (inverted — EUR is 57.6% of DXY).
    EUR/USD rising  = DXY falling  = bullish gold  → aligns with BUY
    EUR/USD falling = DXY rising   = bearish gold  → aligns with SELL
    Returns True if DXY aligns with signal, False if conflict.
    """
    if not CONFIG.get("dxy_enabled") or len(dxy_candles_15m) < 3:
        return True   # no data = pass through

    recent    = dxy_candles_15m[-3:]
    eurusd_move = recent[-1]["close"] - recent[0]["open"]
    # Invert: EUR/USD rising means DXY falling
    dxy_trend = -eurusd_move

    if signal_direction == "BUY" and dxy_trend > 0.0010:
        log.info(f"[DXY] Conflict — EUR/USD falling → DXY rising ({dxy_trend:+.5f}), filtering BUY signal")
        return False
    if signal_direction == "SELL" and dxy_trend < -0.0010:
        log.info(f"[DXY] Conflict — EUR/USD rising → DXY falling ({dxy_trend:+.5f}), filtering SELL signal")
        return False

    return True
