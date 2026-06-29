"""
app.py — GoldEngine Flask application
Dashboard + OAuth + Engine control + API endpoints
"""
import os
import logging
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, redirect, request, jsonify, session
from config import CONFIG
import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "goldengine-dev-secret")

# ── Startup ───────────────────────────────────────────────────────────────────

def startup():
    db.init()

    from upstox_auth import restore_token_on_startup
    if restore_token_on_startup():
        from instrument_resolver import resolve_all
        resolve_all()

    from xauusd_feed import seed_buffers, start_polling
    seed_buffers()
    start_polling()

    # Start intelligence poller (newsbot integration)
    if CONFIG.get("newsbot_url"):
        from intelligence_client import start_polling as start_intel_polling
        start_intel_polling()
        log.info("[Startup] Intelligence client started")

    from upstox_client import start_goldten_ws, start_usdinr_poller, start_background_updaters
    start_goldten_ws()
    start_usdinr_poller()
    start_background_updaters()   # <-- ADDED: updates balance every minute

    # Restore position from DB
    pos = db.get("current_position")
    if pos:
        CONFIG["current_position"] = pos
        log.info("[Startup] Restored open position from DB")

    log.info("[Startup] GoldEngine ready")


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login")
@app.route("/auth/login")
def login():
    from upstox_auth import get_login_url, _api_key
    if not _api_key():
        return "<h2>UPSTOX_API_KEY not configured</h2>", 400
    return redirect(get_login_url())


@app.route("/callback")
@app.route("/auth/callback")
def callback():
    from upstox_auth import exchange_code_for_token, save_token
    code  = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"<h2>OAuth Error: {error}</h2><a href='/login'>Retry</a>", 400
    if not code:
        return "<h2>No auth code</h2>", 400
    token = exchange_code_for_token(code)
    if not token:
        return "<h2>Token exchange failed — check logs</h2><a href='/login'>Retry</a>", 500
    save_token(token)
    from instrument_resolver import resolve_all
    resolve_all()
    # Auto-start engine after successful auth
    from gold_engine import get_engine
    eng = get_engine()
    if not eng._running:
        eng.start()
    return redirect("/")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


# ── Positions API (with debug logging) ───────────────────────────────────────

@app.route("/api/positions")
def api_positions():
    """
    Returns current short-term positions from Upstox.
    Falls back to the engine's current_position if the Upstox call fails.
    """
    from upstox_client import get_positions
    positions = get_positions()
    log.info(f"[DEBUG /api/positions] response type: {type(positions)}, count: {len(positions)}")
    if positions:
        log.info(f"[DEBUG /api/positions] first position: {positions[0]}")
    else:
        log.warning("[DEBUG /api/positions] No positions returned from Upstox")

    # ── Normalize Upstox V2 field names → dashboard-expected names ──────────
    # Upstox short-term-positions returns:
    #   quantity            (net, day+overnight combined)
    #   average_price       ✓
    #   pnl                 (day realised P&L)
    #   unrealised          (unrealised P&L if position open)
    #   day_buy_quantity / day_sell_quantity  (intraday trades only)
    #   overnight_buy_quantity / overnight_sell_quantity
    # Dashboard JS expects: net_quantity, average_price, day_pnl, unrealised
    goldten_last = CONFIG.get("goldten_last", 0)
    for p in positions:
        # ── net_quantity ──────────────────────────────────────────────────────
        # Upstox field is "quantity" (net day+overnight). Map to net_quantity
        # so JS doesn't need to know both names.
        if "net_quantity" not in p:
            p["net_quantity"] = int(p.get("quantity", 0) or 0)

        # ── average_price ─────────────────────────────────────────────────────
        # Upstox `average_price` is a known buggy field that returns 0 for
        # overnight positions (community-confirmed).  Resolution order:
        #   1. buy_price   — Upstox's own weighted avg price field (most accurate)
        #   2. average_price — if non-zero
        #   3. buy_value / quantity — calculated fallback
        buy_price  = float(p.get("buy_price",    0) or 0)
        avg_price  = float(p.get("average_price", 0) or 0)
        buy_val    = float(p.get("buy_value",     0) or 0)
        qty_f      = float(p.get("quantity",      0) or 0)
        if buy_price > 0:
            p["average_price"] = round(buy_price, 2)
        elif avg_price > 0:
            p["average_price"] = round(avg_price, 2)
        elif buy_val > 0 and qty_f > 0:
            p["average_price"] = round(buy_val / qty_f, 2)

        # ── day_pnl ───────────────────────────────────────────────────────────
        # Upstox returns "pnl" = day realised P&L on short-term-positions.
        if "day_pnl" not in p:
            p["day_pnl"] = float(p.get("pnl", 0) or 0)

        # ── last_price ────────────────────────────────────────────────────────
        sym = (p.get("tradingsymbol") or "").upper()
        if "GOLDTEN" in sym and not p.get("last_price"):
            p["last_price"] = goldten_last

        # ── unrealised ────────────────────────────────────────────────────────
        # Use Upstox's own unrealised if present, otherwise compute.
        if not p.get("unrealised") and p.get("last_price") and p.get("average_price"):
            qty  = int(p.get("net_quantity", 0))
            ltp  = float(p["last_price"])
            avg  = float(p["average_price"])
            mult = 1 if qty >= 0 else -1
            p["unrealised"] = round((ltp - avg) * abs(qty) * mult, 2)

    return jsonify(positions)


# ── LTP for a single instrument ───────────────────────────────────────────────

@app.route("/api/ltp")
def api_ltp():
    """
    GET /api/ltp?instrument_key=MCX_FO|...
    Returns { ltp, change, change_pct } for the requested instrument.
    """
    import requests as req
    instrument_key = request.args.get("instrument_key", "").strip()
    if not instrument_key:
        return jsonify({"error": "instrument_key required"}), 400

    # Shortcut — return cached value for the ACTIVE GOLDTEN contract only
    if "GOLDTEN" in instrument_key.upper() and instrument_key == CONFIG.get("goldten_instrument_key", ""):
        ltp = CONFIG.get("goldten_last", 0)
        return jsonify({"ltp": ltp, "change": 0, "change_pct": 0})

    token = CONFIG.get("upstox_access_token", "")
    if not token:
        return jsonify({"error": "not authenticated"}), 401

    try:
        r = req.get(
            "https://api.upstox.com/v2/market-quote/ltp",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Api-Version": "2.0"},
            params={"instrument_key": instrument_key},
            timeout=6,
        )
        if r.status_code != 200:
            return jsonify({"error": f"upstox {r.status_code}"}), 502
        data = r.json().get("data", {})
        item = data.get(instrument_key) or next(iter(data.values()), {})
        ltp = item.get("last_price", 0)
        return jsonify({"ltp": ltp, "change": 0, "change_pct": 0})
    except Exception as e:
        log.warning(f"[api/ltp] {e}")
        return jsonify({"error": str(e)}), 500


# ── Instrument search ─────────────────────────────────────────────────────────

@app.route("/api/search-instrument")
def api_search_instrument():
    """
    GET /api/search-instrument?query=GOLDTEN&exchange=MCX
    Proxies the Upstox instruments/search endpoint.
    """
    import requests as req
    query    = request.args.get("query", "").strip()
    exchange = request.args.get("exchange", "ALL").strip()
    if not query:
        return jsonify({"data": []}), 200

    token = CONFIG.get("upstox_access_token", "")
    if not token:
        return jsonify({"error": "not authenticated"}), 401

    try:
        params = {"query": query}
        if exchange and exchange != "ALL":
            params["exchange"] = exchange

        r = req.get(
            "https://api.upstox.com/v2/instruments/search",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Api-Version": "2.0"},
            params=params,
            timeout=8,
        )
        if r.status_code != 200:
            return jsonify({"error": f"upstox {r.status_code}", "data": []}), 502
        return jsonify(r.json())
    except Exception as e:
        log.warning(f"[api/search-instrument] {e}")
        return jsonify({"error": str(e), "data": []}), 500


# ── Manual order placement ────────────────────────────────────────────────────

@app.route("/api/manual-order", methods=["POST"])
def api_manual_order():
    """
    POST /api/manual-order
    Body: { direction, instrument_key, qty, price, order_type, product }

    Places an order via Upstox and returns { order_id } or { error }.
    This route is intentionally separate from the engine's automated orders
    so it's always available regardless of engine state.
    """
    import requests as req
    data = request.json or {}

    direction      = data.get("direction", "").upper()          # BUY | SELL
    instrument_key = data.get("instrument_key", "").strip()
    qty            = int(data.get("qty", 1))
    price          = float(data.get("price", 0))
    order_type     = data.get("order_type", "LIMIT").upper()    # LIMIT | MARKET | SL | SL-M
    product        = data.get("product", "D").upper()           # D=NRML/Delivery, I=Intraday

    if direction not in ("BUY", "SELL"):
        return jsonify({"error": "direction must be BUY or SELL"}), 400
    if not instrument_key:
        return jsonify({"error": "instrument_key required"}), 400
    if qty < 1:
        return jsonify({"error": "qty must be >= 1"}), 400

    token = CONFIG.get("upstox_access_token", "")
    if not token:
        return jsonify({"error": "Not authenticated — visit /login first"}), 401

    payload = {
        "instrument_key":     instrument_key,
        "transaction_type":   direction,
        "order_type":         order_type,
        "product":            product,
        "quantity":           qty,
        "price":              round(price, 0) if order_type == "LIMIT" else 0,
        "validity":           "DAY",
        "disclosed_quantity": 0,
        "trigger_price":      0,
        "is_amo":             False,
    }

    try:
        r = req.post(
            "https://api.upstox.com/v2/order/place",
            headers={
                "Authorization":  f"Bearer {token}",
                "Accept":         "application/json",
                "Content-Type":   "application/json",
                "Api-Version":    "2.0",
            },
            json=payload,
            timeout=10,
        )
        resp = r.json()
        if r.status_code == 200:
            order_id = resp.get("data", {}).get("order_id")
            log.info(f"[ManualOrder] {direction} {qty}x {instrument_key} @ {price} → {order_id}")
            return jsonify({"order_id": order_id, "ok": True})
        else:
            err_msg = resp.get("message") or resp.get("error") or r.text[:200]
            log.warning(f"[ManualOrder] failed {r.status_code}: {err_msg}")
            return jsonify({"error": err_msg}), 400
    except Exception as e:
        log.error(f"[ManualOrder] exception: {e}")
        return jsonify({"error": str(e)}), 500


# ── Engine control ────────────────────────────────────────────────────────────

@app.route("/engine/start", methods=["POST"])
def engine_start():
    if not CONFIG.get("upstox_access_token"):
        return jsonify({"error": "No Upstox token — visit /login first"}), 400
    from gold_engine import get_engine
    get_engine().start()
    return jsonify({"status": "started"})


@app.route("/engine/stop", methods=["POST"])
def engine_stop():
    from gold_engine import get_engine
    get_engine().stop()
    return jsonify({"status": "stopped"})


@app.route("/engine/status")
def engine_status():
    from gold_engine import get_engine
    return jsonify(get_engine().get_status())


@app.route("/engine/force-exit", methods=["POST"])
def force_exit():
    from gold_engine import get_engine
    ok = get_engine().force_exit()
    return jsonify({"ok": ok})


@app.route("/engine/kill-switch", methods=["POST"])
def kill_switch():
    CONFIG["kill_switch"] = True
    db.set("kill_switch", True)
    from telegram_alerts import send_message
    send_message("🚨 <b>Kill switch activated</b> — GoldEngine will not enter new trades.")
    return jsonify({"kill_switch": True})


@app.route("/engine/reset-kill-switch", methods=["POST"])
def reset_kill_switch():
    CONFIG["kill_switch"] = False
    db.set("kill_switch", False)
    return jsonify({"kill_switch": False})


# ── Config updates from dashboard ─────────────────────────────────────────────

@app.route("/config/update", methods=["POST"])
def update_config():
    data = request.json or {}
    allowed_keys = {
        "swing_level_threshold_pct", "swing_max_age_hours",
        "risk_reward", "risk_pct", "max_lots",
        "dxy_enabled", "usdinr_trend_enabled", "paper_mode",
        "htf_pivot_left", "htf_pivot_right",
        "ltf_pivot_left", "ltf_pivot_right",
    }
    updated = {}
    for k, v in data.items():
        if k in allowed_keys:
            CONFIG[k] = v
            updated[k] = v
    db.set("config_overrides", updated)
    return jsonify({"updated": updated})


# ── Data API ──────────────────────────────────────────────────────────────────

@app.route("/api/goldten-expiries")
def api_goldten_expiries():
    """
    GET /api/goldten-expiries
    Returns the next 3 GOLDTEN MCX futures expiries with their LTPs.
    Used by the dashboard expiry dropdown.
    """
    import requests as req
    from instrument_resolver import search_instrument

    token = CONFIG.get("upstox_access_token", "")
    if not token:
        return jsonify({"error": "not authenticated", "expiries": []}), 401

    try:
        # Search for GOLDTEN futures — returns all available expiries
        results = search_instrument(query="GOLDTEN", exchanges="MCX", segments="COMM")
        futs = [r for r in results if r.get("instrument_type") == "FUT"]
        futs.sort(key=lambda x: x.get("trading_symbol", ""))
        futs = futs[:3]  # next 3 expiries

        if not futs:
            return jsonify({"expiries": []})

        # Batch LTP fetch
        keys = "|".join(f["instrument_key"] for f in futs)
        ltp_map = {}
        try:
            r = req.get(
                "https://api.upstox.com/v2/market-quote/ltp",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "Api-Version": "2.0"},
                params={"instrument_key": keys},
                timeout=6,
            )
            if r.status_code == 200:
                data = r.json().get("data", {})
                for k, v in data.items():
                    # Upstox returns keys with : instead of |
                    norm = k.replace(":", "|")
                    ltp_map[norm] = v.get("last_price", 0)
        except Exception as e:
            log.warning(f"[api/goldten-expiries] LTP fetch error: {e}")

        expiries = []
        for f in futs:
            sym = f.get("trading_symbol", "")  # e.g. GOLDTEN26JULFUT
            key = f["instrument_key"]
            # Build a readable label like "JUL26 FUT" from trading_symbol
            label = sym  # fallback
            if sym.startswith("GOLDTEN") and sym.endswith("FUT"):
                middle = sym[7:-3]  # e.g. "26JUL"
                if len(middle) >= 5:
                    year = middle[:2]
                    month = middle[2:]
                    label = f"MCX {month}{year}"
            ltp = ltp_map.get(key, 0)
            # Fallback: use cached GOLDTEN price for the active key
            if not ltp and key == CONFIG.get("goldten_instrument_key", ""):
                ltp = CONFIG.get("goldten_last", 0)
            expiries.append({"label": label, "instrument_key": key, "trading_symbol": sym, "ltp": ltp})

        return jsonify({"expiries": expiries})

    except Exception as e:
        log.warning(f"[api/goldten-expiries] {e}")
        return jsonify({"error": str(e), "expiries": []}), 500


@app.route("/api/status")
def api_status():
    from gold_engine import get_engine
    status = get_engine().get_status()
    log.info(f"[DEBUG /api/status] balance={status.get('balance')}, goldten_last={status.get('goldten_last')}")
    return jsonify(status)


@app.route("/api/trades")
def api_trades():
    trades = db.get_recent_trades(50)
    return jsonify([dict(t) for t in trades])


@app.route("/api/swings")
def api_swings():
    from gold_engine import get_engine
    return jsonify(get_engine().swings.all_levels())


@app.route("/api/conversion")
def api_conversion():
    """Live conversion calc — useful for manual verification."""
    from upstox_client import xau_to_mcx, get_usdinr
    xau       = CONFIG.get("xauusd_last", 0)
    usdinr    = get_usdinr()
    mcx_equiv = xau_to_mcx(xau) if xau else 0
    return jsonify({
        "xauusd":          xau,
        "usdinr":          usdinr,               # Upstox NCD_FO futures rate (frozen after 17:00)
        "usdinr_spot":     CONFIG.get("usdinr_spot", 0),   # TwelveData forex spot (continuous)
        "usdinr_frozen":   CONFIG.get("usdinr_is_frozen"),
        "oz_to_10gms":     CONFIG.get("oz_to_10gms"),
        "mcx_equiv":       mcx_equiv,
        "goldten_last":    CONFIG.get("goldten_last"),
        "live_basis":      CONFIG.get("live_basis"),
        "basis_pct":       round(CONFIG.get("live_basis", 0) / mcx_equiv * 100, 3) if mcx_equiv else 0,
        "wti_last":        CONFIG.get("wti_last", 0),      # WTI crude spot (USD/bbl)
    })


# ── Newsbot Intelligence ──────────────────────────────────────────────────────

@app.route("/api/intelligence")
def api_intelligence():
    """
    Returns the latest XAU intelligence signal fetched from the newsbot.
    Cached in CONFIG["intelligence"] by intelligence_client.py.
    """
    intel = CONFIG.get("intelligence", {})
    return jsonify(intel)


# ── Macro Events Calendar ────────────────────────────────────────────────────

@app.route("/api/macro-events")
def api_macro_events():
    """
    Returns upcoming macro events for the dashboard events card.
    Cached for 1 hour in macro_calendar.py.
    Optional query params:
      days=30  — look-ahead window in days (default 30)
    """
    from macro_calendar import get_macro_events
    days   = min(int(request.args.get("days", 30)), 90)
    token  = os.getenv("FINNHUB_TOKEN", "")  # optional — falls back to static
    events = get_macro_events(finnhub_token=token, max_days=days)
    return jsonify({"events": events, "count": len(events)})


    
@app.route("/api/myip")
def api_myip():
    import requests as req
    r = req.get("https://api.ipify.org?format=json", timeout=5)
    return jsonify(r.json())

# ── Cron endpoints (Railway cron) ─────────────────────────────────────────────

@app.route("/cron/start", methods=["POST"])
def cron_start():
    from gold_engine import get_engine
    eng = get_engine()
    if CONFIG.get("upstox_access_token") and not eng._running:
        eng.start()
    return "ok", 200


@app.route("/cron/stop", methods=["POST"])
def cron_stop():
    from gold_engine import get_engine
    get_engine().stop()
    return "ok", 200


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    startup()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


# ── Gunicorn-compatible startup ───────────────────────────────────────────────
# Called whether running via `python app.py` or `gunicorn app:app`
with app.app_context():
    startup()