"""
upstox_client.py — Upstox V2 API wrapper for GoldEngine.

All functions read CONFIG for auth token and cached state.
Key fix (UDAPI1026): place_order() now validates instrument_key before
sending to Upstox, with an automatic live re-resolve attempt if it's empty.

USDINR change: Upstox NSE futures poller replaced with Twelve Data /price
endpoint via xauusd_feed.fetch_usdinr_twelvedata(). No IP whitelist needed.
"""
import logging
import requests
import threading
import time as _time
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
# Source: Twelve Data /price endpoint via xauusd_feed.py
# Upstox NSE futures dependency removed — no longer blocked by IP whitelist.

def get_usdinr() -> float:
    """
    Return the current USDINR rate.
    Priority:
      1. usdinr_frozen  — set at 17:00 IST, held for evening MCX session
      2. usdinr_live    — live rate cached by Twelve Data poll loop (every 30s)
      3. fresh fetch    — immediate Twelve Data call if cache is empty
    Returns 0.0 only if all three are unavailable.
    """
    if CONFIG.get("usdinr_is_frozen"):
        frozen = float(CONFIG.get("usdinr_frozen", 0) or 0)
        if frozen > 0:
            return frozen
    live = float(CONFIG.get("usdinr_live", 0) or 0)
    if live > 0:
        return live
    # Cache empty — fetch fresh from Twelve Data immediately
    from xauusd_feed import fetch_usdinr_twelvedata
    return fetch_usdinr_twelvedata()


def fetch_usdinr_ltp() -> float:
    """
    Compatibility shim — previously fetched from Upstox NSE futures.
    Now delegates to Twelve Data. All call sites unchanged.
    """
    from xauusd_feed import fetch_usdinr_twelvedata
    return fetch_usdinr_twelvedata()


# ── XAU → MCX conversion ──────────────────────────────────────────────────────

def xau_to_mcx(xau_price: float) -> float:
    """
    Convert XAU/USD spot price ($/oz) to MCX GOLDTEN equivalent (₹/10gms).
    Formula: XAU × 0.35274 × USDINR
    USDINR source priority: frozen → Twelve Data live → fresh Twelve Data fetch
    """
    usdinr = get_usdinr()
    if usdinr <= 0:
        log.warning("[upstox_client] xau_to_mcx: USDINR unavailable from all sources")
        return 0.0
    source = (
        "frozen"    if CONFIG.get("usdinr_is_frozen") and float(CONFIG.get("usdinr_frozen", 0) or 0) > 0
        else "live"
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
                    "instrument_key":   key,
                    "quantity":         qty,
                    "transaction_type": "BUY",
                    "product":          "D",   # NRML
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
        data  = r.json().get("data", {})
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
    Respects CONFIG["max_lots"] cap. Uses 90% of balance (10% buffer).
    """
    if margin_per_lot <= 0:
        return 0
    usable    = balance * 0.90
    raw_lots  = int(usable // margin_per_lot)
    max_cap   = int(CONFIG.get("max_lots", 5))
    risk_pct  = float(CONFIG.get("risk_pct", 2.0))
    capital   = float(CONFIG.get("capital", balance))
    risk_lots = max(1, int((capital * risk_pct / 100) // margin_per_lot)) if capital > 0 else raw_lots
    return max(0, min(raw_lots, risk_lots, max_cap))


# ── Order placement ───────────────────────────────────────────────────────────

def place_order(
    direction:      str,
    qty:            int,
    price:          float,
    order_type:     str = "LIMIT",
    product:        str = "D",
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
    """
    # ── 1. Resolve instrument key ─────────────────────────────────────────────
    if not instrument_key:
        instrument_key = CONFIG.get("goldten_instrument_key", "")

    if not instrument_key:
        log.warning("[place_order] instrument_key empty — attempting live re-resolve")
        try:
            from instrument_resolver import ensure_goldten_key
            instrument_key = ensure_goldten_key()
        except Exception as e:
            log.error(f"[place_order] re-resolve import failed: {e}")

    if not instrument_key:
        log.error(
            "[place_order] ❌ UDAPI1026 guard: instrument_key still empty after re-resolve. "
            "Re-Auth via dashboard to refresh the token and instrument key."
        )
        return None

    if "|" not in instrument_key:
        log.warning(
            f"[place_order] instrument_key '{instrument_key}' looks wrong "
            "(expected 'MCX_FO|<token>'). Proceeding anyway."
        )

    # ── 2. Validate inputs ────────────────────────────────────────────────────
    if qty < 1:
        log.error(f"[place_order] qty={qty} invalid — must be ≥ 1")
        return None

    direction  = direction.upper()
    if direction not in ("BUY", "SELL"):
        log.error(f"[place_order] direction='{direction}' invalid")
        return None

    order_type = order_type.upper()
    if order_type == "MARKET":
        price = 0

    # ── 3. Build payload ──────────────────────────────────────────────────────
    payload = {
        "instrument_token":   instrument_key,   # Upstox V2 field name
        "transaction_type":   direction,
        "order_type":         order_type,
        "product":            product,
        "quantity":           qty,
        "price":              round(float(price), 2) if order_type == "LIMIT" else 0,
        "trigger_price":      0,
        "disclosed_quantity": 0,
        "validity":           "DAY",
        "is_amo":             False,
        "slice":              False,
        "tag":                "GoldEngine",
    }

    log.info(
        f"[place_order] Sending {direction} {qty}× GOLDTEN @ ₹{price:.0f} "
        f"[{order_type}] key={instrument_key}"
    )

    # ── 4. Send to Upstox ─────────────────────────────────────────────────────
    try:
        r    = requests.post(f"{BASE}/order/place", headers=_json_headers(),
                             json=payload, timeout=10)
        resp = r.json()

        if r.status_code == 200 and resp.get("status") == "success":
            order_id = resp.get("data", {}).get("order_id", "")
            log.info(f"[place_order] ✅ Order placed: {order_id}")
            return order_id

        errors  = resp.get("errors", [])
        err_msg = "; ".join(
            f"{e.get('errorCode','?')}: {e.get('message','?')}" for e in errors
        ) if errors else resp.get("message", str(resp))

        log.error(
            f"[place_order] ❌ Order rejected [{r.status_code}]: {err_msg}\n"
            f"  Payload: {payload}"
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
    Computes net_quantity from overnight + day_buy - day_sell since
    Upstox V2 does not return it directly.
    Fixes average_price=0 for pure short positions by falling back to sell_price.
    Returns [] on error.
    """
    try:
        r = requests.get(f"{BASE}/portfolio/short-term-positions",
                         headers=_headers(), timeout=8)
        if r.status_code != 200:
            log.warning(f"[upstox_client] positions API {r.status_code}")
            return []
        data = r.json().get("data", []) or []
        for p in data:
            o   = int(p.get("overnight_quantity", 0) or 0)
            db  = int(p.get("day_buy_quantity",   0) or 0)
            ds  = int(p.get("day_sell_quantity",  0) or 0)
            net = o + db - ds
            p["net_quantity"] = net
            if float(p.get("average_price", 0) or 0) == 0 and net < 0:
                p["average_price"] = float(p.get("sell_price", 0) or 0)
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
    """Fetch GOLDTEN MCX futures LTP and cache in CONFIG."""
    key = CONFIG.get("goldten_instrument_key", "")
    if not key:
        return float(CONFIG.get("goldten_last", 0))
    ltp = get_ltp(key)
    if ltp > 0:
        CONFIG["goldten_last"] = ltp
    return ltp


# ── Background pollers ────────────────────────────────────────────────────────

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
    Start a background thread that refreshes USDINR every 30 seconds
    using Twelve Data /price endpoint (replaces Upstox NSE futures poller).
    Does an immediate first fetch so usdinr_live is populated before
    the first xau_to_mcx call.
    """
    def _loop():
        from xauusd_feed import fetch_usdinr_twelvedata
        # Immediate first fetch — no startup window with usdinr_live=0
        try:
            rate = fetch_usdinr_twelvedata()
            if rate > 0:
                log.info(f"[upstox_client] USDINR initial fetch: {rate:.4f} (Twelve Data)")
        except Exception as e:
            log.warning(f"[upstox_client] usdinr initial fetch error: {e}")
        while True:
            _time.sleep(30)
            try:
                fetch_usdinr_twelvedata()
            except Exception as e:
                log.warning(f"[upstox_client] usdinr poll error: {e}")

    t = threading.Thread(target=_loop, name="usdinr-ltp-poller", daemon=True)
    t.start()
    log.info("[upstox_client] USDINR poller started (Twelve Data, 30s interval)")


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