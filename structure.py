"""
structure.py — Market structure analysis on XAU/USD candles
  - HTF 15m: BOS pivot bias detection (BULLISH / BEARISH / NONE)
  - LTF 5m:  BOS/CHoCH entry signal
  - SwingLevelTracker: stores confirmed swings, checks MCX proximity

Fixes:
  - _last_scan_ts removed — was blocking duplicate scan prevention incorrectly,
    causing stale levels to persist and new pivots to be skipped
  - Duplicate detection tightened to < 0.1 XAU (was 0.5 — too loose)
  - usdinr=0 guard: skip saving swing if USDINR not yet available
  - mcx_equiv on existing levels refreshed when usdinr updates
  - SwingLevelTracker.load_from_db() now clears stale levels by age
    and re-scans immediately after seed instead of waiting 30s
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
        self.ref_high      = None
        self.ref_low       = None
        self.ref_high_ts   = None
        self.ref_low_ts    = None
        self.last_bos_ts   = None

    def update(self, candles: list) -> str:
        if len(candles) < 10:
            return self.bias

        left  = CONFIG.get("htf_pivot_left",  3)
        right = CONFIG.get("htf_pivot_right", 3)
        n     = len(candles)

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

        last_close = candles[-1]["close"]
        last_ts    = candles[-1]["timestamp"]

        if self.ref_high and last_close > self.ref_high:
            if self.last_bos_ts != last_ts:
                log.info(f"[HTF] BOS BULLISH — close {last_close:.3f} > pivot high {self.ref_high:.3f}")
                self.bias        = "BULLISH"
                self.last_bos_ts = last_ts
                self.ref_high    = None

        elif self.ref_low and last_close < self.ref_low:
            if self.last_bos_ts != last_ts:
                log.info(f"[HTF] BOS BEARISH — close {last_close:.3f} < pivot low {self.ref_low:.3f}")
                self.bias        = "BEARISH"
                self.last_bos_ts = last_ts
                self.ref_low     = None

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
        if len(candles) < 8 or htf_bias == "NONE":
            return None

        left  = CONFIG.get("ltf_pivot_left",  2)
        right = CONFIG.get("ltf_pivot_right", 2)
        n     = len(candles)

        for idx in range(max(0, n - right - 3), n - right):
            if find_pivot_high(candles, idx, left, right):
                ph = candles[idx]["high"]
                if self.ref_high is None or ph != self.ref_high:
                    self.ref_high = ph
            if find_pivot_low(candles, idx, left, right):
                pl = candles[idx]["low"]
                if self.ref_low is None or pl != self.ref_low:
                    self.ref_low = pl

        last       = candles[-1]
        last_close = last["close"]
        last_ts    = last["timestamp"]

        if last_ts == self.last_signal_ts:
            return None

        if htf_bias == "BULLISH" and self.ref_high and last_close > self.ref_high:
            self.last_signal_ts = last_ts
            self.ref_high       = None
            log.info(f"[LTF] BUY signal @ {last_close:.3f}")
            return {
                "direction": "BUY",
                "xau_price": last_close,
                "xau_stop":  self.ref_low or (last_close * 0.995),
                "candle_ts": last_ts,
            }

        if htf_bias == "BEARISH" and self.ref_low and last_close < self.ref_low:
            self.last_signal_ts = last_ts
            self.ref_low        = None
            log.info(f"[LTF] SELL signal @ {last_close:.3f}")
            return {
                "direction": "SELL",
                "xau_price": last_close,
                "xau_stop":  self.ref_high or (last_close * 1.005),
                "candle_ts": last_ts,
            }

        return None


# ── Swing Level Tracker ───────────────────────────────────────────────────────

class SwingLevelTracker:
    """
    Detects confirmed swing H/L on 15m XAU/USD, converts to MCX equiv,
    and checks if GOLDTEN price is within threshold% for confluence.

    Key fixes vs previous version:
    - Removed _last_scan_ts: was incorrectly blocking candles in the same scan
      pass after the first pivot was found, causing missed pivots and stale
      levels never being replaced.
    - Tightened duplicate detection to < 0.1 XAU (was 0.5).
    - Hard guard: skip saving any swing if usdinr <= 0 (prevents ₹0 entries).
    - load_from_db() purges levels outside max_age window so stale DB records
      from yesterday don't pollute today's dashboard.
    - refresh_mcx_equivs(): called by passive analysis after USDINR becomes
      available — fixes any ₹0 MCX equiv entries saved during startup race.
    """

    def __init__(self):
        self._levels    = []
        self._seen_ts   = set()   # set of (timestamp, swing_type) already processed

    def load_from_db(self):
        """
        Restore recent swing levels from DB on startup.
        Purges any level outside swing_max_age_hours window.
        """
        max_age = CONFIG.get("swing_max_age_hours", 48)
        raw     = list(db.get_active_swings(max_age))

        # Filter out corrupted levels (usdinr=0 or mcx_equiv=0)
        clean = [s for s in raw
                 if float(s.get("usdinr_rate", 0) or 0) > 0
                 and float(s.get("mcx_equiv",   0) or 0) > 0]

        if len(raw) != len(clean):
            log.warning(
                f"[SwingTracker] Dropped {len(raw) - len(clean)} corrupted swing levels "
                f"(usdinr=0 or mcx_equiv=0) from DB load"
            )

        self._levels  = clean
        self._seen_ts = {
            (str(s.get("timestamp", "")), s.get("swing_type", ""))
            for s in clean
        }
        log.info(f"[SwingTracker] Loaded {len(self._levels)} clean swing levels from DB")

    def scan(self, candles_15m: list, usdinr: float):
        """
        Scan 15m candles for new confirmed pivot H/L.
        Skips scan entirely if USDINR is not yet available (prevents ₹0 storage).
        """
        if len(candles_15m) < 10:
            return

        # Hard guard — never store a level with usdinr=0
        if usdinr <= 0:
            log.debug("[SwingTracker] scan skipped — USDINR not yet available")
            return

        left  = CONFIG.get("htf_pivot_left",  3)
        right = CONFIG.get("htf_pivot_right", 3)
        n     = len(candles_15m)
        oz    = CONFIG.get("oz_to_10gms", 0.35274)
        max_n = CONFIG.get("swing_max_levels", 20)

        for idx in range(max(0, n - right - 5), n - right):
            c  = candles_15m[idx]
            ts = c["timestamp"]

            # Pivot HIGH
            if find_pivot_high(candles_15m, idx, left, right):
                xau_p   = c["high"]
                seen_key = (str(ts), "swing_high")
                # Skip if already processed this exact candle+type
                if seen_key in self._seen_ts:
                    continue
                # Skip if close price duplicate already in levels (< 0.1 XAU)
                existing = [s for s in self._levels
                            if s.get("swing_type") == "swing_high"
                            and abs(float(s.get("xau_price", 0)) - xau_p) < 0.1]
                if not existing:
                    mcx_eq   = round(xau_p * oz * usdinr, 0)
                    swing    = {
                        "xau_price":   xau_p,
                        "mcx_equiv":   mcx_eq,
                        "usdinr_rate": usdinr,
                        "swing_type":  "swing_high",
                        "timestamp":   ts,
                        "touched":     False,
                    }
                    swing_id     = db.save_swing(swing)
                    swing["id"]  = swing_id
                    self._levels.append(swing)
                    self._seen_ts.add(seen_key)
                    log.info(
                        f"[SwingTracker] NEW HIGH: XAU={xau_p:.3f} "
                        f"→ MCX≈₹{mcx_eq:,.0f} (USDINR={usdinr:.4f})"
                    )

            # Pivot LOW
            if find_pivot_low(candles_15m, idx, left, right):
                xau_p    = c["low"]
                seen_key = (str(ts), "swing_low")
                if seen_key in self._seen_ts:
                    continue
                existing = [s for s in self._levels
                            if s.get("swing_type") == "swing_low"
                            and abs(float(s.get("xau_price", 0)) - xau_p) < 0.1]
                if not existing:
                    mcx_eq   = round(xau_p * oz * usdinr, 0)
                    swing    = {
                        "xau_price":   xau_p,
                        "mcx_equiv":   mcx_eq,
                        "usdinr_rate": usdinr,
                        "swing_type":  "swing_low",
                        "timestamp":   ts,
                        "touched":     False,
                    }
                    swing_id     = db.save_swing(swing)
                    swing["id"]  = swing_id
                    self._levels.append(swing)
                    self._seen_ts.add(seen_key)
                    log.info(
                        f"[SwingTracker] NEW LOW:  XAU={xau_p:.3f} "
                        f"→ MCX≈₹{mcx_eq:,.0f} (USDINR={usdinr:.4f})"
                    )

        # Trim to max_n most recent levels
        if len(self._levels) > max_n:
            self._levels = self._levels[-max_n:]

    def refresh_mcx_equivs(self, usdinr: float):
        """
        Recalculate MCX equiv for any level that was saved with usdinr=0
        (startup race condition where scan ran before USDINR was available).
        Called by passive analysis after USDINR first becomes available.
        """
        if usdinr <= 0:
            return
        oz      = CONFIG.get("oz_to_10gms", 0.35274)
        updated = 0
        for lvl in self._levels:
            if float(lvl.get("mcx_equiv", 0) or 0) == 0:
                xau_p         = float(lvl.get("xau_price", 0))
                mcx_eq        = round(xau_p * oz * usdinr, 0)
                lvl["mcx_equiv"]   = mcx_eq
                lvl["usdinr_rate"] = usdinr
                if lvl.get("id"):
                    db.update_swing_mcx(lvl["id"], mcx_eq, usdinr)
                updated += 1
        if updated:
            log.info(f"[SwingTracker] Refreshed MCX equiv for {updated} level(s) after USDINR available")

    def nearby_levels(self, goldten_price: float) -> list:
        threshold = CONFIG.get("swing_level_threshold_pct", 0.30)
        nearby    = []
        for lvl in self._levels:
            if lvl.get("touched"):
                continue
            mcx_eq = float(lvl.get("mcx_equiv", 0))
            if mcx_eq <= 0:
                continue
            dist_pct = abs(goldten_price - mcx_eq) / mcx_eq * 100
            if dist_pct <= threshold:
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
    Real DXY confluence filter using CONFIG["dxy_last"] and CONFIG["dxy_change_pct"]
    sourced from newsbot /api/prices/ (DX-Y.NYB via yfinance).

    DXY rising  → USD strengthening → bearish gold → conflicts with BUY
    DXY falling → USD weakening     → bullish gold → conflicts with SELL
    Threshold: ±0.15% change is considered directional.
    """
    if not CONFIG.get("dxy_enabled"):
        return True

    dxy_price      = CONFIG.get("dxy_last", 0.0)
    dxy_change_pct = CONFIG.get("dxy_change_pct", 0.0)

    if not dxy_price:
        if len(dxy_candles_15m) >= 3:
            recent      = dxy_candles_15m[-3:]
            eurusd_move = recent[-1]["close"] - recent[0]["open"]
            dxy_trend   = -eurusd_move
            if signal_direction == "BUY"  and dxy_trend >  0.0010:
                log.info(f"[DXY] Fallback EUR/USD — conflict filtering BUY ({dxy_trend:+.5f})")
                return False
            if signal_direction == "SELL" and dxy_trend < -0.0010:
                log.info(f"[DXY] Fallback EUR/USD — conflict filtering SELL ({dxy_trend:+.5f})")
                return False
        return True

    if signal_direction == "BUY"  and dxy_change_pct >  0.15:
        log.info(f"[DXY] DXY rising {dxy_price:.2f} (+{dxy_change_pct:.3f}%) — filtering BUY")
        return False
    if signal_direction == "SELL" and dxy_change_pct < -0.15:
        log.info(f"[DXY] DXY falling {dxy_price:.2f} ({dxy_change_pct:.3f}%) — filtering SELL")
        return False

    log.debug(f"[DXY] {dxy_price:.2f} ({dxy_change_pct:+.3f}%) — no conflict for {signal_direction}")
    return True
