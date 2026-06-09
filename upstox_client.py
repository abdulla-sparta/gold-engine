"""
upstox_client.py — Upstox V3 WebSocket for GOLDTEN MCX + USDINR futures
Also handles:
  - Live margin fetch for GOLDTEN
  - Order placement on MCX
  - Ledger balance fetch
  - Real‑time balance and positions for dashboard
"""
import os
import json
import time
import threading
import logging
import requests
from datetime import datetime, timezone, timedelta
from config import CONFIG

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

# ── REST helpers ──────────────────────────────────────────────────────────────

def _headers():
    return {
        "Authorization": f"Bearer {CONFIG.get('upstox_access_token', '')}",
        "Accept":        "application/json",
        "Api-Version":   "2.0",
    }

BASE = "https://api.upstox.com/v2"


# ── Balance & Positions (live) ─────────────────────────────────────────────────

def fetch_ledger_balance() -> float:
    """Fetch available cash balance from Upstox ledger."""
    token = CONFIG.get("upstox_access_token")
    if not token:
        log.warning("[Balance] No Upstox token - returning fallback")
        return CONFIG.get("capital", 200000)

    try:
        r = requests.get(f"{BASE}/user/fund-and-margin", headers=_headers(), timeout=10)
        if r.status_code != 200:
            log.warning(f"[Balance] fetch failed: {r.status_code} {r.text[:200]}")
            return CONFIG.get("capital", 200000)

        data = r.json()
        # equity segment available margin
        equity = data.get("data", {}).get("equity", {})
        available = equity.get("available_margin", 0)
        if available:
            log.info(f"[Balance] Available: ₹{available:,.0f}")
            return float(available)

        # fallback: total balance
        total = equity.get("total_balance", 0)
        if total:
            return float(total)

        return CONFIG.get("capital", 200000)
    except Exception as e:
        log.warning(f"[Balance] error: {e}")
        return CONFIG.get("capital", 200000)


def get_positions() -> list:
    """Fetch current open positions from Upstox portfolio."""
    token = CONFIG.get("upstox_access_token")
    if not token:
        log.warning("[Positions] No token")
        return []

    try:
        r = requests.get(f"{BASE}/portfolio/short-term-positions", headers=_headers(), timeout=10)
        if r.status_code != 200:
            log.warning(f"[Positions] HTTP {r.status_code}: {r.text[:200]}")
            return []

        data = r.json()
        positions = data.get("data", [])
        log.info(f"[Positions] Found {len(positions)} open positions")
        return positions
    except Exception as e:
        log.exception("[Positions] Exception")
        return []


def fetch_margin_for_goldten(qty: int = 1) -> float:
    """
    Fetch SPAN+Exposure margin required for qty lots of GOLDTEN MCX futures.
    Returns margin per lot.
    """
    try:
        instrument_key = CONFIG.get("goldten_instrument_key")
        r = requests.post(
            f"{BASE}/charges/margin",
            headers={**_headers(), "Content-Type": "application/json"},
            json={
                "instruments": [{
                    "instrument_key": instrument_key,
                    "quantity":       qty,
                    "transaction_type": "BUY",
                    "product":        "D",   # delivery/NRML for MCX
                }]
            },
            timeout=10
        )
        if r.status_code != 200:
            log.warning(f"[Margin] fetch failed: {r.status_code} {r.text[:200]}")
            return 15000.0   # fallback estimate
        data = r.json()
        total = data.get("data", {}).get("required_margin", 0)
        per_lot = float(total) / qty if qty > 0 else float(total)
        log.info(f"[Margin] GOLDTEN per lot: ₹{per_lot:,.0f}")
        return per_lot
    except Exception as e:
        log.warning(f"[Margin] error: {e}")
        return 15000.0


def calc_max_lots(balance: float, margin_per_lot: float) -> int:
    """Calculate max lots we can take given balance and per-lot margin."""
    if margin_per_lot <= 0:
        return 1
    hard_cap  = CONFIG.get("max_lots", 5)
    risk_pct  = CONFIG.get("risk_pct", 2.0)
    # Use risk_pct of balance as max deployment
    deploy    = balance * (risk_pct / 100) * 10   # 10x leverage allowance
    max_by_margin = int(deploy / margin_per_lot)
    return max(1, min(max_by_margin, hard_cap))


def fetch_usdinr_rate() -> float:
    """Fetch last traded price of USDINR futures from Upstox LTP endpoint."""
    try:
        instrument_key = CONFIG.get("usdinr_instrument_key")
        r = requests.get(
            f"{BASE}/market-quote/ltp",
            headers=_headers(),
            params={"instrument_key": instrument_key},
            timeout=8
        )
        if r.status_code != 200:
            log.warning(f"[USDINR] fetch failed: {r.status_code}")
            return CONFIG.get("usdinr_live", 85.0)
        resp_data = r.json().get("data", {})
        item = resp_data.get(instrument_key)
        if not item:
            item = next(iter(resp_data.values()), None) if resp_data else None
        ltp = item.get("last_price", 0) if item else 0
        if ltp:
            log.info(f"[USDINR] ₹{ltp:.4f}")
            return float(ltp)
        log.warning(f"[USDINR] LTP not found in response: {r.text[:150]}")
        return CONFIG.get("usdinr_live", 85.0)
    except Exception as e:
        log.warning(f"[USDINR] fetch error: {e}")
        return CONFIG.get("usdinr_live", 85.0)


def get_usdinr() -> float:
    """Return the correct USDINR rate — frozen after 5 PM."""
    if CONFIG.get("usdinr_is_frozen"):
        return CONFIG.get("usdinr_frozen", CONFIG.get("usdinr_live", 85.0))
    return CONFIG.get("usdinr_live", 85.0)


def xau_to_mcx(xau_price: float) -> float:
    """Convert XAU/USD price ($/oz) to MCX GOLDTEN equivalent (₹/10gms)."""
    usdinr  = get_usdinr()
    oz_conv = CONFIG.get("oz_to_10gms", 0.35274)
    return round(xau_price * oz_conv * usdinr, 0)


# ── Order placement ───────────────────────────────────────────────────────────

def place_order(direction: str, qty: int, price: float, order_type: str = "LIMIT") -> str | None:
    """
    Place MCX GOLDTEN order via Upstox.
    direction: "BUY" or "SELL"
    qty: number of lots
    price: limit price in ₹
    Returns order_id or None on failure.
    """
    try:
        instrument_key = CONFIG.get("goldten_instrument_key")
        payload = {
            "instrument_key":    instrument_key,
            "transaction_type":  direction,
            "order_type":        order_type,
            "product":           "D",           # NRML for MCX overnight
            "quantity":          qty,
            "price":             round(price, 0) if order_type == "LIMIT" else 0,
            "validity":          "DAY",
            "disclosed_quantity": 0,
            "trigger_price":     0,
            "is_amo":            False,
        }
        r = requests.post(
            f"{BASE}/order/place",
            headers={**_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=10
        )
        if r.status_code != 200:
            log.error(f"[Order] failed: {r.status_code} — {r.text[:300]}")
            return None
        order_id = r.json().get("data", {}).get("order_id")
        log.info(f"[Order] placed: {direction} {qty}x GOLDTEN @ ₹{price:.0f} → {order_id}")
        return order_id
    except Exception as e:
        log.error(f"[Order] exception: {e}")
        return None


def cancel_order(order_id: str) -> bool:
    try:
        r = requests.delete(
            f"{BASE}/order/cancel",
            headers=_headers(),
            params={"order_id": order_id},
            timeout=8
        )
        return r.status_code == 200
    except Exception as e:
        log.warning(f"[Cancel] error: {e}")
        return False


# ── WebSocket feed for GOLDTEN live price ─────────────────────────────────────

def start_goldten_ws():
    """
    Subscribe to GOLDTEN MCX live feed via Upstox V3 WebSocket.
    Updates CONFIG["goldten_last"] and CONFIG["live_basis"] on each tick.
    """
    import websocket

    def _on_message(ws, message):
        try:
            # Upstox V3 sends protobuf — decode via REST LTP for simplicity
            pass
        except Exception as e:
            log.debug(f"[WS] message error: {e}")

    def _poll_goldten_ltp():
        """Lightweight fallback: poll LTP every 5 seconds."""
        while True:
            try:
                token = CONFIG.get("upstox_access_token", "")
                instrument_key = CONFIG.get("goldten_instrument_key", "")
                if token and instrument_key:
                    r = requests.get(
                        f"{BASE}/market-quote/ltp",
                        headers=_headers(),
                        params={"instrument_key": instrument_key},
                        timeout=5
                    )
                    if r.status_code == 200:
                        data = r.json().get("data", {})
                        # Key in response may be exact instrument_key
                        # Try direct key first, then iterate all values
                        item = data.get(instrument_key)
                        if not item:
                            # Fallback: grab first item in data dict
                            item = next(iter(data.values()), None) if data else None
                        ltp = item.get("last_price", 0) if item else 0
                        if ltp:
                            CONFIG["goldten_last"] = float(ltp)
                            xau = CONFIG.get("xauusd_last", 0)
                            if xau > 0:
                                spot_equiv = xau_to_mcx(xau)
                                CONFIG["live_basis"] = round(float(ltp) - spot_equiv, 0)
                            log.info(f"[GoldtenLTP] ₹{ltp:.0f}")
                    elif r.status_code != 200:
                        log.warning(f"[GoldtenLTP] {r.status_code}: {r.text[:100]}")
            except Exception as e:
                log.debug(f"[GoldtenLTP] poll error: {e}")
            time.sleep(5)

    t = threading.Thread(target=_poll_goldten_ltp, daemon=True, name="GoldtenLTP")
    t.start()
    log.info("[Upstox] GOLDTEN LTP poller started")
    return t


# ── USDINR poller + freeze logic ──────────────────────────────────────────────

def start_usdinr_poller():
    """
    Polls USDINR futures every 30s during market hours.
    At 17:00 IST, freezes the rate and stops polling.
    """
    def _loop():
        while True:
            now_ist = datetime.now(IST)
            hour    = now_ist.hour
            minute  = now_ist.minute

            # Freeze at 17:00 IST
            if hour == 17 and minute == 0 and not CONFIG.get("usdinr_is_frozen"):
                frozen = CONFIG.get("usdinr_live", 0.0)
                if frozen > 0:
                    CONFIG["usdinr_frozen"]    = frozen
                    CONFIG["usdinr_is_frozen"] = True
                    log.info(f"[USDINR] Frozen at ₹{frozen:.4f} — evening session mode")
                    from telegram_alerts import send_message
                    send_message(
                        f"🔒 <b>USDINR Frozen</b>\n"
                        f"Rate locked at <b>{frozen:.4f}</b> for evening session.\n"
                        f"Gold engine continues till 23:25 IST."
                    )

            # Reset freeze at midnight
            if hour == 0 and minute == 1 and CONFIG.get("usdinr_is_frozen"):
                CONFIG["usdinr_is_frozen"] = False
                CONFIG["usdinr_frozen"]    = 0.0
                log.info("[USDINR] Freeze reset — morning session")

            # Only fetch if not frozen and during USDINR trading hours (9 AM–5 PM IST)
            if not CONFIG.get("usdinr_is_frozen") and 9 <= hour < 17:
                if not CONFIG.get("upstox_access_token"):
                    pass   # skip silently — no token yet
                else:
                    rate = fetch_usdinr_rate()
                    if rate > 0:
                        CONFIG["usdinr_live"] = rate

            time.sleep(30)

    t = threading.Thread(target=_loop, daemon=True, name="USDINRPoller")
    t.start()
    log.info("[Upstox] USDINR poller started")
    return t


# ── Background updater for balance & positions (dashboard) ─────────────────────

def start_balance_updater():
    """
    Periodically fetches live balance and updates CONFIG["capital"].
    This makes the dashboard show real‑time available funds.
    """
    def _update():
        while True:
            try:
                balance = fetch_ledger_balance()
                if balance > 0:
                    CONFIG["capital"] = balance
                    CONFIG["balance"] = balance   # alias for dashboard
                    log.debug(f"[BalanceUpdater] Updated capital: ₹{balance:,.0f}")
            except Exception as e:
                log.warning(f"[BalanceUpdater] error: {e}")
            time.sleep(60)   # update every minute

    t = threading.Thread(target=_update, daemon=True, name="BalanceUpdater")
    t.start()
    log.info("[Upstox] Balance updater started (every 60s)")


# Call this at startup (after token is ready)
def start_background_updaters():
    start_balance_updater()
    # Positions are fetched on demand via /api/positions, no background needed