"""
config.py — GoldEngine configuration
All runtime-tunable values live here. Dashboard can update SWING_LEVEL_THRESHOLD.
"""
import os

CONFIG = {
    # ── Identity ──────────────────────────────────────────────────────────────
    "app_name": "GoldEngine",

    # ── Upstox credentials ────────────────────────────────────────────────────
    "upstox_api_key":    os.getenv("UPSTOX_API_KEY", ""),
    "upstox_api_secret": os.getenv("UPSTOX_API_SECRET", ""),
    "upstox_redirect_uri": os.getenv("UPSTOX_REDIRECT_URI", ""),
    "upstox_access_token": "",   # filled at runtime after OAuth

    # ── Twelve Data — 3-key rotation ─────────────────────────────────────────
    # Set TWELVE_DATA_API_KEY, TWELVE_DATA_API_KEY_2, TWELVE_DATA_API_KEY_3
    # in Railway env vars.  Keys are tried in order; a rate-limit response
    # marks a key exhausted for 60 s then automatically falls back.
    "twelve_data_api_key":   os.getenv("TWELVE_DATA_API_KEY", ""),
    "twelve_data_api_key_2": os.getenv("TWELVE_DATA_API_KEY_2", ""),
    "twelve_data_api_key_3": os.getenv("TWELVE_DATA_API_KEY_3", ""),
    "xauusd_poll_interval": 60,   # seconds — free tier safe

    # ── Instruments ───────────────────────────────────────────────────────────
    "goldten_instrument_key": "",   # resolved at startup via instrument_resolver
    "usdinr_instrument_key":  "",   # resolved at startup via instrument_resolver

    # ── Conversion constants ──────────────────────────────────────────────────
    "oz_to_10gms": 0.35274,          # 1 troy oz = 31.1035g → 10g = 0.35274 oz

    # ── Strategy parameters ───────────────────────────────────────────────────
    "htf_candles":            "15min",    # XAU/USD HTF timeframe
    "ltf_candles":            "5min",     # XAU/USD LTF timeframe
    "htf_pivot_left":         3,          # pivot confirmation bars each side
    "htf_pivot_right":        3,
    "ltf_pivot_left":         2,
    "ltf_pivot_right":        2,
    "htf_buffer_size":        100,        # max candles kept in memory
    "ltf_buffer_size":        200,

    # ── Swing level tracker ───────────────────────────────────────────────────
    "swing_level_threshold_pct": 0.30,    # ±% MCX must be near swing equiv
    "swing_max_age_hours":       48,      # discard swings older than this
    "swing_max_levels":          10,      # keep latest N swing levels

    # ── Risk & sizing ─────────────────────────────────────────────────────────
    "risk_reward":            5.0,        # minimum RR per trade
    "risk_pct":               2.0,        # % of available balance risked per trade
    "max_lots":               5,          # hard cap regardless of margin calc
    "capital":                200000,     # starting balance (₹), overridden by live ledger

    # ── Sessions ──────────────────────────────────────────────────────────────
    "morning_session_start": "09:00",    # IST
    "morning_session_end":   "17:00",    # IST — USDINR futures close
    "evening_session_end":   "23:25",    # IST — MCX gold close (5 min buffer)
    "usdinr_freeze_time":    "17:00",    # IST — after this, use frozen rate

    # ── Confluence filters ────────────────────────────────────────────────────
    "dxy_enabled":            True,       # use DXY as confluence filter
    "dxy_symbol":             "DX-Y.NYB", # Real DXY from newsbot /api/prices/ (yfinance)
    "usdinr_trend_enabled":   True,

    # ── Telegram ──────────────────────────────────────────────────────────────
    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id":   os.getenv("TELEGRAM_CHAT_ID", ""),

    # ── Newsbot Intelligence ──────────────────────────────────────────────────
    "newsbot_url":                 os.getenv("NEWSBOT_URL", ""),
    "intelligence_poll_interval":  1800,   # seconds — 30 min
    "intelligence_filter_enabled": True,

    # ── Runtime state (not persisted) ────────────────────────────────────────
    "engine_running":      False,
    "usdinr_live":         0.0,
    "usdinr_frozen":       0.0,          # set at 5 PM
    "usdinr_is_frozen":    False,
    "xauusd_last":         0.0,
    "dxy_last":            0.0,
    "dxy_change_pct":      0.0,
    "goldten_last":        0.0,
    "live_basis":          0.0,
    "current_position":    None,
    "kill_switch":         False,
    "paper_mode":          True,
    "intelligence":        {},
}
