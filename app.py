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
    restore_token_on_startup()

    from xauusd_feed import seed_buffers, start_polling
    seed_buffers()
    start_polling()

    from upstox_client import start_goldten_ws, start_usdinr_poller
    start_goldten_ws()
    start_usdinr_poller()

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
        "dxy_enabled", "usdinr_trend_enabled",
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

@app.route("/api/status")
def api_status():
    from gold_engine import get_engine
    return jsonify(get_engine().get_status())


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
    xau   = CONFIG.get("xauusd_last", 0)
    usdinr = get_usdinr()
    mcx_equiv = xau_to_mcx(xau) if xau else 0
    return jsonify({
        "xauusd":          xau,
        "usdinr":          usdinr,
        "usdinr_frozen":   CONFIG.get("usdinr_is_frozen"),
        "oz_to_10gms":     CONFIG.get("oz_to_10gms"),
        "mcx_equiv":       mcx_equiv,
        "goldten_last":    CONFIG.get("goldten_last"),
        "live_basis":      CONFIG.get("live_basis"),
        "basis_pct":       round(CONFIG.get("live_basis", 0) / mcx_equiv * 100, 3) if mcx_equiv else 0,
    })


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
