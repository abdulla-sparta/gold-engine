"""
upstox_client.py — Upstox V2 API wrapper for GoldEngine.

All functions read CONFIG for auth token and cached state.
Key fix (UDAPI1026): place_order() now validates instrument_key before
sending to Upstox, with an automatic live re-resolve attempt if it's empty.
"""
import logging
import requests
from datetime import datetime, timezone, timedelta
from config import CONFIG

log = logging.getLogger(__name__)
BASE  = "https://api.upstox.com/v2"
IST   = timezone(timedelta(hours=5, minutes=30))

# Oz → 10gms conversion factor (1 troy oz = 31.1035g, contract = 10g)
OZ_TO_10G = 0.35274


# ── Auth header ───────────────────────────────────────────────────────────────

def _headers() -> dict:
    token = CONFIG.get("upstox_access_token", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
    }

def _json_headers() -> dict:
    return {**_headers(), "Content-Type": "application/json"}


# ── USDINR ────────────────────────────────────────────────────────────────────

def get_usdinr() -> float:
    """
    Return the current USDINR rate.
    Priority:
      1. usdinr_frozen  — set at 17:00 IST from last Upstox NCD_FO LTP
      2. usdinr_live    — live Upstox NCD_FO futures LTP (polled every 30s)
      3. usdinr_spot    — TwelveData USD/INR forex spot (fallback when market
                          is closed or futures poller hasn't run yet)
    Returns 0.0 only if all three are unavailable.
    """
    if CONFIG.get("usdinr_is_frozen"):
        frozen = float(CONFIG.get("usdinr_frozen", 0) or 0)
        if frozen > 0:
            return frozen
    live = float(CONFIG.get("usdinr_live", 0) or 0)
    if live > 0:
        return live
    # Fallback: TwelveData forex spot (continuous, not frozen at 17:00)
    spot = float(CONFIG.get("usdinr_spot", 0) or 0)
    return spot


def fetch_usdinr_ltp() -> float:
    """Fetch live USDINR futures LTP from Upstox and cache in CONFIG."""
    key = CONFIG.get("usdinr_instrument_key", "")
    if not key:
        log.debug("[upstox_client] USDINR instrument key not resolved yet")
        return 0.0
    try:
        r = requests.get(
            f"{BASE}/market-quote/ltp",
            headers=_headers(),
            params={"instrument_key": key},
            timeout=8,
        )
        if r.status_code != 200:
            log.warning(f"[upstox_client] USDINR LTP fetch failed {r.status_code}")
            return 0.0
        data = r.json().get("data", {})
        # Response: { "data": { "<key>": { "last_price": ... } } }
        ltp = 0.0
        for v in data.values():
            ltp = float(v.get("last_price", 0) or 0)
            break
        if ltp > 0:
            CONFIG["usdinr_live"] = ltp
        return ltp
    except Exception as e:
        log.warning(f"[upstox_client] fetch_usdinr_ltp error: {e}")
        return 0.0


# ── XAU → MCX conversion ──────────────────────────────────────────────────────

def xau_to_mcx(xau_price: float) -> float:
    """
    Convert XAU/USD spot price ($/oz) to MCX GOLDTEN equivalent (₹/10gms).
    Formula: XAU × 0.35274 × USDINR
    USDINR source priority: frozen → Upstox NCD_FO live → TwelveData spot
    """
    usdinr = get_usdinr()
    if usdinr <= 0:
        log.warning("[upstox_client] xau_to_mcx: USDINR unavailable from all sources")
        return 0.0
    # Log source at DEBUG level so we can see which rate is being used
    source = (
        "frozen" if CONFIG.get("usdinr_is_frozen") and float(CONFIG.get("usdinr_frozen", 0) or 0) > 0
        else "upstox_futures" if float(CONFIG.get("usdinr_live", 0) or 0) > 0
        else "td_spot"
    )
    log.debug(f"[upstox_client] xau_to_mcx: USDINR={usdinr:.4f} ({source})")
    return xau_price * OZ_TO_10G * usdinr


# ── Account / margin ──────────────────────────────────────────────────────────

def fetch_ledger_balance() -> float:
    """
    Fetch available cash balance from Upstox funds API.
    Falls back to CONFIG["capital"] if the API call fails.
    """
    try:
        r = requests.get(f"{BASE}/user/get-funds-and-margin",
                         headers=_headers(), timeout=8)
        if r.status_code != 200:
            log.warning(f"[upstox_client] funds API {r.status_code} — using CONFIG capital")
            return float(CONFIG.get("capital", 0))
        d = r.json().get("data", {})
        # equity.available_margin or commodity.available_margin
        commodity = d.get("commodity", {})
        equity    = d.get("equity", {})
        bal = float(
            commodity.get("available_margin")
            or equity.get("available_margin")
            or CONFIG.get("capital", 0)
        )
        CONFIG["balance"] = bal
        return bal
    except Exception as e:
        log.warning(f"[upstox_client] fetch_ledger_balance error: {e}")
        return float(CONFIG.get("capital", 0))


def fetch_margin_for_goldten(qty: int = 1) -> float:
    """
    Fetch the SPAN+exposure margin required for qty lots of GOLDTEN.
    Falls back to CONFIG["goldten_margin_per_lot"] if set, else 15000.
    """
    key = CONFIG.get("goldten_instrument_key", "")
    if not key:
        return float(CONFIG.get("goldten_margin_per_lot", 15000))
    try:
        payload = {
            "instruments": [
                {
                    "instrument_key": key,
                    "quantity":       qty,
                    "transaction_type": "BUY",
                    "product":        "D",   # NRML
                }
            ]
        }
        r = requests.post(
            f"{BASE}/charges/margin",
            headers=_json_headers(),
            json=payload,
            timeout=8,
        )
        if r.status_code != 200:
            log.warning(f"[upstox_client] margin API {r.status_code}")
            return float(CONFIG.get("goldten_margin_per_lot", 15000))
        data = r.json().get("data", {})
        total = float(data.get("required_margin", 0) or 0)
        if total > 0:
            CONFIG["goldten_margin_per_lot"] = total
        return total or float(CONFIG.get("goldten_margin_per_lot", 15000))
    except Exception as e:
        log.warning(f"[upstox_client] fetch_margin_for_goldten error: {e}")
        return float(CONFIG.get("goldten_margin_per_lot", 15000))


def calc_max_lots(balance: float, margin_per_lot: float) -> int:
    """
    Calculate max lots tradeable given balance and per-lot margin.
    Respects CONFIG["max_lots"] cap.
    Uses 90% of balance as usable margin (10% buffer).
    """
    if margin_per_lot <= 0:
        return 0
    usable    = balance * 0.90
    raw_lots  = int(usable // margin_per_lot)
    max_cap   = int(CONFIG.get("max_lots", 5))
    risk_pct  = float(CONFIG.get("risk_pct", 2.0))
    # Also cap by risk % of capital
    capital   = float(CONFIG.get("capital", balance))
    risk_lots = max(1, int((capital * risk_pct / 100) // margin_per_lot)) if capital > 0 else raw_lots
    return max(0, min(raw_lots, risk_lots, max_cap))


# ── Order placement ───────────────────────────────────────────────────────────

def place_order(
    direction:  str,
    qty:        int,
    price:      float,
    order_type: str = "LIMIT",
    product:    str = "D",
    instrument_key: str = None,
) -> str | None:
    """
    Place a BUY or SELL order on Upstox for GOLDTEN MCX futures.

    Args:
        direction:      "BUY" or "SELL"
        qty:            number of lots
        price:          limit price in ₹ (ignored for MARKET orders)
        order_type:     "LIMIT" | "MARKET" | "SL" | "SL-M"
        product:        "D" (NRML) | "I" (Intraday)
        instrument_key: override key (uses CONFIG["goldten_instrument_key"] if None)

    Returns:
        order_id string on success, None on failure.

    Fix: validates instrument_key before sending — attempts a live re-resolve
    if CONFIG key is empty, then aborts with None (not an empty-string order).
    """
    # ── 1. Resolve instrument key ─────────────────────────────────────────────
    if not instrument_key:
        instrument_key = CONFIG.get("goldten_instrument_key", "")

    if not instrument_key:
        # Last-resort live re-resolve (catches post-startup auth refresh)
        log.warning("[place_order] instrument_key empty — attempting live re-resolve before abort")
        try:
            from instrument_resolver import ensure_goldten_key
            instrument_key = ensure_goldten_key()
        except Exception as e:
            log.error(f"[place_order] re-resolve import failed: {e}")

    if not instrument_key:
        log.error(
            "[place_order] ❌ UDAPI1026 guard: instrument_key is empty after re-resolve. "
            "Cannot place order. Re-Auth via dashboard to refresh the token and instrument key."
        )
        return None

    # Sanity-check format (Upstox V2 format: "MCX_FO|<token>" or "NSE_FO|<token>")
    if "|" not in instrument_key:
        log.warning(
            f"[place_order] instrument_key '{instrument_key}' looks wrong "
            "(expected 'MCX_FO|<token>'). Proceeding anyway."
        )

    # ── 2. Validate other inputs ──────────────────────────────────────────────
    if qty < 1:
        log.error(f"[place_order] qty={qty} is invalid — must be ≥ 1")
        return None

    direction = direction.upper()
    if direction not in ("BUY", "SELL"):
        log.error(f"[place_order] direction='{direction}' invalid")
        return None

    order_type = order_type.upper()
    # For MARKET orders, Upstox requires price=0
    if order_type == "MARKET":
        price = 0

    # ── 3. Build payload ──────────────────────────────────────────────────────
    payload = {
        "instrument_token":  instrument_key,   # Upstox V2 field name
        "transaction_type":  direction,
        "order_type":        order_type,
        "product":           product,
        "quantity":          qty,
        "price":             round(float(price), 2) if order_type == "LIMIT" else 0,
        "trigger_price":     0,
        "disclosed_quantity": 0,
        "validity":          "DAY",
        "is_amo":            False,
        "slice":             False,
        "tag":               "GoldEngine",
    }

    log.info(
        f"[place_order] Sending {direction} {qty}× GOLDTEN @ ₹{price:.0f} "
        f"[{order_type}] key={instrument_key}"
    )

    # ── 4. Send to Upstox ─────────────────────────────────────────────────────
    try:
        r = requests.post(
            f"{BASE}/order/place",
            headers=_json_headers(),
            json=payload,
            timeout=10,
        )
        resp = r.json()

        if r.status_code == 200 and resp.get("status") == "success":
            order_id = resp.get("data", {}).get("order_id", "")
            log.info(f"[place_order] ✅ Order placed: {order_id}")
            return order_id

        # Extract error details for logging
        errors  = resp.get("errors", [])
        err_msg = "; ".join(
            f"{e.get('errorCode','?')}: {e.get('message','?')}" for e in errors
        ) if errors else resp.get("message", str(resp))

        log.error(
            f"[place_order] ❌ Order rejected [{r.status_code}]: {err_msg}\n"
            f"  Payload sent: {payload}"
        )
        return None

    except requests.exceptions.Timeout:
        log.error("[place_order] Request timed out")
        return None
    except Exception as e:
        log.error(f"[place_order] Unexpected error: {e}")
        return None


# ── Positions ─────────────────────────────────────────────────────────────────

def get_positions() -> list:
    """
    Fetch all open positions from Upstox.
    Returns a list of position dicts, or [] on error.
    """
    try:
        r = requests.get(f"{BASE}/portfolio/short-term-positions",
                         headers=_headers(), timeout=8)
        if r.status_code != 200:
            log.warning(f"[upstox_client] positions API {r.status_code}")
            return []
        data = r.json().get("data", []) or []
        return data
    except Exception as e:
        log.warning(f"[upstox_client] get_positions error: {e}")
        return []


def get_ltp(instrument_key: str) -> float:
    """Fetch last traded price for a single instrument key."""
    if not instrument_key:
        return 0.0
    try:
        r = requests.get(
            f"{BASE}/market-quote/ltp",
            headers=_headers(),
            params={"instrument_key": instrument_key},
            timeout=6,
        )
        if r.status_code != 200:
            return 0.0
        data = r.json().get("data", {})
        for v in data.values():
            return float(v.get("last_price", 0) or 0)
        return 0.0
    except Exception:
        return 0.0


def get_goldten_ltp() -> float:
    """Convenience wrapper — fetch GOLDTEN MCX futures LTP and cache in CONFIG."""
    key = CONFIG.get("goldten_instrument_key", "")
    if not key:
        return float(CONFIG.get("goldten_last", 0))
    ltp = get_ltp(key)
    if ltp > 0:
        CONFIG["goldten_last"] = ltp
    return ltp


# ── Background pollers ─────────────────────────────────────────────────────────
import threading
import time as _time


def start_goldten_ws():
    """
    Start a background thread that polls GOLDTEN LTP every 5 seconds.
    Named *_ws for API compatibility; uses REST polling (no WebSocket needed).
    """
    def _loop():
        while True:
            try:
                get_goldten_ltp()
            except Exception as e:
                log.warning(f"[upstox_client] goldten ltp poll error: {e}")
            _time.sleep(5)

    t = threading.Thread(target=_loop, name="goldten-ltp-poller", daemon=True)
    t.start()
    log.info("[upstox_client] GOLDTEN LTP poller started (5s interval)")


def start_usdinr_poller():
    """
    Start a background thread that refreshes USDINR LTP every 30 seconds.
    Also does an immediate first fetch so usdinr_live is populated before
    the first xau_to_mcx call.  If Upstox returns 0 (market closed / key
    not ready), the TwelveData usdinr_spot fallback in get_usdinr() covers it.
    """
    def _loop():
        # Immediate first attempt — eliminates the startup window where
        # usdinr_live=0 causes repeated xau_to_mcx warnings
        try:
            fetch_usdinr_ltp()
        except Exception as e:
            log.warning(f"[upstox_client] usdinr initial fetch error: {e}")
        while True:
            _time.sleep(30)
            try:
                fetch_usdinr_ltp()
            except Exception as e:
                log.warning(f"[upstox_client] usdinr poll error: {e}")

    t = threading.Thread(target=_loop, name="usdinr-ltp-poller", daemon=True)
    t.start()
    log.info("[upstox_client] USDINR LTP poller started (30s interval, immediate first fetch)")


def start_background_updaters():
    """Start a background thread that refreshes ledger balance every 60 seconds."""
    def _loop():
        while True:
            try:
                fetch_ledger_balance()
            except Exception as e:
                log.warning(f"[upstox_client] balance updater error: {e}")
            _time.sleep(60)

    t = threading.Thread(target=_loop, name="balance-updater", daemon=True)
    t.start()
    log.info("[upstox_client] Balance updater started (60s interval)")
