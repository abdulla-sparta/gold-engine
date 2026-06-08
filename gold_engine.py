"""
gold_engine.py — Main trading engine for GoldEngine
Orchestrates: XAU/USD structure → swing level check → MCX order placement
"""
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

from config import CONFIG
import db
from xauusd_feed import get_xauusd, get_dxy
from upstox_client import (
    xau_to_mcx, get_usdinr, fetch_ledger_balance,
    fetch_margin_for_goldten, calc_max_lots, place_order, get_positions
)
from structure import HTFBiasEngine, LTFEntryEngine, SwingLevelTracker, dxy_confluence
from telegram_alerts import send_message

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


class GoldEngine:

    def __init__(self):
        self.htf      = HTFBiasEngine()
        self.ltf      = LTFEntryEngine()
        self.swings   = SwingLevelTracker()
        self._running = False
        self._thread  = None
        self._last_candle_ts = None

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        CONFIG["engine_running"] = True
        self.swings.load_from_db()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="GoldEngine")
        self._thread.start()
        log.info("[GoldEngine] Started")
        send_message("🚀 <b>GoldEngine started</b>\nMonitoring XAU/USD structure for MCX entries.")

    def stop(self):
        self._running = False
        CONFIG["engine_running"] = False
        log.info("[GoldEngine] Stopped")
        send_message("🔴 <b>GoldEngine stopped</b>")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                self._tick()
            except Exception as e:
                log.error(f"[GoldEngine] tick error: {e}")
            time.sleep(15)   # check every 15s (candles update every 60s)

    def _tick(self):
        now_ist = datetime.now(IST)

        # Session guard — only trade during MCX gold hours
        if not self._is_trading_session(now_ist):
            return

        # Kill switch
        if CONFIG.get("kill_switch"):
            return

        # Already in a position — check exit
        if CONFIG.get("current_position"):
            self._check_exit()
            return

        xau_feed = get_xauusd()
        dxy_feed = get_dxy()

        candles_15m = xau_feed.buf_15m.all_closed()
        candles_5m  = xau_feed.buf_5m.all_closed()

        if len(candles_15m) < 10 or len(candles_5m) < 8:
            return

        # Only process on new 5m candle close
        latest_5m_ts = candles_5m[-1]["timestamp"]
        if latest_5m_ts == self._last_candle_ts:
            return
        self._last_candle_ts = latest_5m_ts

        # 1. Update swing level tracker (runs on 15m)
        usdinr = get_usdinr()
        self.swings.scan(candles_15m, usdinr)

        # 2. HTF bias
        htf_bias = self.htf.update(candles_15m)

        # 3. LTF entry signal
        signal = self.ltf.check(candles_5m, htf_bias)
        if not signal:
            return

        # 4. Swing level confluence — must be near a key level
        goldten = CONFIG.get("goldten_last", 0)
        if goldten <= 0:
            return

        nearby = self.swings.nearby_levels(goldten)
        if not nearby:
            log.debug(f"[GoldEngine] Signal {signal['direction']} — not near any swing level, skip")
            return

        # Pick the closest level
        best_level = min(nearby, key=lambda x: x["dist_pct"])

        # 5. Direction must match swing type
        direction = signal["direction"]
        if direction == "SELL" and best_level["swing_type"] != "swing_high":
            log.debug("[GoldEngine] SELL near swing_low — skip (wrong level type)")
            return
        if direction == "BUY" and best_level["swing_type"] != "swing_low":
            log.debug("[GoldEngine] BUY near swing_high — skip (wrong level type)")
            return

        # 6. DXY confluence
        dxy_candles = dxy_feed.buf_15m.all_closed()
        if not dxy_confluence(dxy_candles, direction):
            return

        # 7. USDINR trend filter
        if CONFIG.get("usdinr_trend_enabled"):
            if not self._usdinr_aligns(direction):
                log.info("[GoldEngine] USDINR trend conflict — skip")
                return

        # 8. Build MCX entry, stop, target
        xau_entry = signal["xau_price"]
        xau_stop  = signal["xau_stop"]

        mcx_entry  = xau_to_mcx(xau_entry)
        mcx_stop   = xau_to_mcx(xau_stop)
        stop_dist  = abs(mcx_entry - mcx_stop)

        if stop_dist < 50:
            log.info(f"[GoldEngine] Stop too tight (₹{stop_dist:.0f}) — skip")
            return

        rr         = CONFIG.get("risk_reward", 5.0)
        mcx_target = (mcx_entry + rr * stop_dist) if direction == "BUY" \
                     else (mcx_entry - rr * stop_dist)

        # 9. Sizing — fetch live margin
        balance       = fetch_ledger_balance()
        margin_per_lot = fetch_margin_for_goldten(qty=1)
        qty           = calc_max_lots(balance, margin_per_lot)

        if qty < 1:
            log.warning("[GoldEngine] Insufficient margin for even 1 lot — skip")
            return

        # 10. Place order
        order_id = place_order(direction, qty, mcx_entry)
        if not order_id:
            log.error("[GoldEngine] Order placement failed")
            return

        # 11. Mark swing level as touched
        if best_level.get("id"):
            self.swings.mark_touched(best_level["id"])

        # 12. Save trade to DB
        trade = {
            "direction":    direction,
            "entry_price":  mcx_entry,
            "stop_price":   mcx_stop,
            "target_price": mcx_target,
            "qty":          qty,
            "xauusd_entry": xau_entry,
            "usdinr_rate":  usdinr,
            "basis":        CONFIG.get("live_basis", 0),
            "notes":        f"swing_{best_level['swing_type']}@{best_level['xau_price']:.2f} dist={best_level['dist_pct']:.2f}%",
        }
        trade_id = db.save_trade(trade)

        CONFIG["current_position"] = {
            **trade,
            "trade_id":  trade_id,
            "order_id":  order_id,
            "open_time": datetime.now(IST).isoformat(),
        }
        db.set("current_position", CONFIG["current_position"])

        # 13. Telegram alert
        frozen_tag = " 🔒frozen" if CONFIG.get("usdinr_is_frozen") else ""
        send_message(
            f"📈 <b>GoldEngine Entry</b>\n\n"
            f"Direction: <b>{direction}</b>\n"
            f"XAU/USD:   <b>${xau_entry:,.3f}</b>\n"
            f"USDINR:    <b>₹{usdinr:.4f}</b>{frozen_tag}\n"
            f"MCX Entry: <b>₹{mcx_entry:,.0f}</b>\n"
            f"MCX Stop:  <b>₹{mcx_stop:,.0f}</b> ({stop_dist:.0f} pts)\n"
            f"MCX Target:<b>₹{mcx_target:,.0f}</b> (RR {rr}:1)\n"
            f"Qty:       <b>{qty} lot(s)</b>\n"
            f"Swing ref: {best_level['swing_type']} @ ${best_level['xau_price']:.2f} "
            f"({best_level['dist_pct']:.2f}% away)\n"
            f"HTF Bias:  {htf_bias}"
        )
        log.info(f"[GoldEngine] Trade opened: {direction} {qty}x @ ₹{mcx_entry:.0f}")

    # ── Exit check ────────────────────────────────────────────────────────────

    def _check_exit(self):
        pos       = CONFIG.get("current_position")
        goldten   = CONFIG.get("goldten_last", 0)
        direction = pos.get("direction")
        stop      = pos.get("stop_price")
        target    = pos.get("target_price")

        if goldten <= 0:
            return

        hit_stop   = (direction == "BUY"  and goldten <= stop)  or \
                     (direction == "SELL" and goldten >= stop)
        hit_target = (direction == "BUY"  and goldten >= target) or \
                     (direction == "SELL" and goldten <= target)

        reason = None
        if hit_stop:
            reason = "STOP"
        elif hit_target:
            reason = "TARGET"

        if reason:
            entry = pos.get("entry_price")
            qty   = pos.get("qty", 1)
            pnl   = (goldten - entry) * qty * (1 if direction == "BUY" else -1)

            # Close via opposite market order
            close_dir = "SELL" if direction == "BUY" else "BUY"
            place_order(close_dir, qty, goldten, order_type="MARKET")

            db.close_trade(pos["trade_id"], round(pnl, 2))
            CONFIG["current_position"] = None
            db.set("current_position", None)

            emoji = "✅" if reason == "TARGET" else "🛑"
            send_message(
                f"{emoji} <b>GoldEngine Exit — {reason}</b>\n\n"
                f"Direction: {direction}\n"
                f"Entry: ₹{entry:,.0f} → Exit: ₹{goldten:,.0f}\n"
                f"PnL: <b>₹{pnl:,.0f}</b>\n"
                f"Qty: {qty} lot(s)"
            )
            log.info(f"[GoldEngine] {reason} exit — PnL ₹{pnl:,.0f}")

    # ── Force exit (manual) ───────────────────────────────────────────────────

    def force_exit(self) -> bool:
        pos = CONFIG.get("current_position")
        if not pos:
            return False
        goldten   = CONFIG.get("goldten_last", 0)
        direction = pos.get("direction")
        qty       = pos.get("qty", 1)
        entry     = pos.get("entry_price")
        close_dir = "SELL" if direction == "BUY" else "BUY"
        place_order(close_dir, qty, goldten, order_type="MARKET")
        pnl = (goldten - entry) * qty * (1 if direction == "BUY" else -1)
        db.close_trade(pos["trade_id"], round(pnl, 2))
        CONFIG["current_position"] = None
        db.set("current_position", None)
        send_message(f"🚨 <b>Manual Exit</b>\nPnL: ₹{pnl:,.0f}")
        return True

    # ── Session check ─────────────────────────────────────────────────────────

    def _is_trading_session(self, now_ist: datetime) -> bool:
        h = now_ist.hour
        m = now_ist.minute
        # MCX gold: 09:00 – 23:25 IST (Mon–Fri), 09:00–14:00 Sat
        if now_ist.weekday() >= 6:   # Sunday
            return False
        if now_ist.weekday() == 5:   # Saturday
            return 9 <= h < 14
        hm = h * 60 + m
        return (9 * 60) <= hm <= (23 * 60 + 25)

    def _usdinr_aligns(self, direction: str) -> bool:
        """USDINR rising = dollar strong = bearish gold in INR terms."""
        live   = CONFIG.get("usdinr_live", 0)
        frozen = CONFIG.get("usdinr_frozen", 0)
        rate   = CONFIG.get("usdinr_live", 0)
        # Simple check: if rate moved >0.2% today, use as trend signal
        # Real trend tracking would need previous day close — kept simple for now
        return True   # pass through, can refine later

    def get_status(self) -> dict:
        return {
            "running":          CONFIG.get("engine_running"),
            "kill_switch":      CONFIG.get("kill_switch"),
            "htf_bias":         self.htf.status(),
            "xauusd_last":      CONFIG.get("xauusd_last"),
            "goldten_last":     CONFIG.get("goldten_last"),
            "usdinr_live":      CONFIG.get("usdinr_live"),
            "usdinr_frozen":    CONFIG.get("usdinr_frozen"),
            "usdinr_is_frozen": CONFIG.get("usdinr_is_frozen"),
            "live_basis":       CONFIG.get("live_basis"),
            "current_position": CONFIG.get("current_position"),
            "swing_levels":     self.swings.all_levels(),
            "swing_threshold":  CONFIG.get("swing_level_threshold_pct"),
            "dxy_last":         CONFIG.get("dxy_last"),
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
_engine = None

def get_engine() -> GoldEngine:
    global _engine
    if _engine is None:
        _engine = GoldEngine()
    return _engine
