"""
upstox_client.py — Upstox V3 WebSocket for GOLDTEN MCX + USDINR futures
Also handles:
  - Live margin fetch for GOLDTEN
  - Order placement on MCX
  - Ledger balance fetch
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


def fetch_ledger_balance() -> float:
    """Fetch available cash balance from Upstox ledger."""
    try:
        r = requests.get(f"{BASE}/user/fund-and-margin", headers=_headers(), timeout=10)
        if r.status_code != 200:
            log.warning(f"[Upstox] ledger fetch failed: {r.status_code}")
            return CONFIG.get("capital", 200000)
        data = r.json()
        # equity segment available margin
        equity = data.get("data", {}).get("equity", {})
        available = equity.get("available_margin", 0)
        if available:
            log.info(f"[Upstox] Available balance: ₹{available:,.0f}")
            return float(available)
        return CONFIG.get("capital", 200000)
    except Exception as e:
        log.warning(f"[Upstox] balance fetch error: {e}")
        return CONFIG.get("capital", 200000)


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
            log.warning(f"[Upstox] margin fetch failed: {r.status_code} {r.text[:200]}")
            return 15000.0   # fallback estimate
        data = r.json()
        total = data.get("data", {}).get("required_margin", 0)
        per_lot = float(total) / qty if qty > 0 else float(total)
        log.info(f"[Upstox] GOLDTEN margin per lot: ₹{per_lot:,.0f}")
        return per_lot
    except Exception as e:
        log.warning(f"[Upstox] margin fetch error: {e}")
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
            log.warning(f"[Upstox] USDINR fetch failed: {r.status_code}")
            return CONFIG.get("usdinr_live", 85.0)
        data = r.json()
        ltp = data.get("data", {}).get(instrument_key, {}).get("last_price", 0)
        if ltp:
            return float(ltp)
        return CONFIG.get("usdinr_live", 85.0)
    except Exception as e:
        log.warning(f"[Upstox] USDINR fetch error: {e}")
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
            log.error(f"[Upstox] Order failed: {r.status_code} — {r.text[:300]}")
            return None
        order_id = r.json().get("data", {}).get("order_id")
        log.info(f"[Upstox] Order placed: {direction} {qty}x GOLDTEN @ ₹{price:.0f} → {order_id}")
        return order_id
    except Exception as e:
        log.error(f"[Upstox] place_order error: {e}")
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
        log.warning(f"[Upstox] cancel_order error: {e}")
        return False


def get_positions() -> list:
    """Fetch current open positions."""
    try:
        r = requests.get(f"{BASE}/portfolio/short-term-positions",
                         headers=_headers(), timeout=8)
        if r.status_code != 200:
            return []
        return r.json().get("data", [])
    except Exception:
        return []


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
        """Lightweight fallback: poll LTP every 5 seconds if WS unavailable."""
        instrument_key = CONFIG.get("goldten_instrument_key")
        while True:
            try:
                if CONFIG.get("upstox_access_token"):
                    r = requests.get(
                        f"{BASE}/market-quote/ltp",
                        headers=_headers(),
                        params={"instrument_key": instrument_key},
                        timeout=5
                    )
                    if r.status_code == 200:
                        data = r.json().get("data", {})
                        ltp  = data.get(instrument_key, {}).get("last_price", 0)
                        if ltp:
                            CONFIG["goldten_last"] = float(ltp)
                            # Update live basis
                            xau  = CONFIG.get("xauusd_last", 0)
                            if xau > 0:
                                spot_equiv = xau_to_mcx(xau)
                                CONFIG["live_basis"] = round(float(ltp) - spot_equiv, 0)
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
                rate = fetch_usdinr_rate()
                if rate > 0:
                    CONFIG["usdinr_live"] = rate

            time.sleep(30)

    t = threading.Thread(target=_loop, daemon=True, name="USDINRPoller")
    t.start()
    log.info("[Upstox] USDINR poller started")
    return t
