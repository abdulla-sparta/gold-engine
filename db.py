"""
db.py — PostgreSQL persistence for GoldEngine
Tables: kv_store, trades, swing_levels, signals
"""
import os
import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)
_conn = None


def _get_conn():
    global _conn
    import psycopg2
    from psycopg2.extras import RealDictCursor
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(os.getenv("DATABASE_URL"), cursor_factory=RealDictCursor)
        _conn.autocommit = True
    return _conn


def init():
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS kv_store (
            key        TEXT PRIMARY KEY,
            value      JSONB,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id           SERIAL PRIMARY KEY,
            symbol       TEXT DEFAULT 'GOLDTEN',
            direction    TEXT,
            entry_price  NUMERIC,
            stop_price   NUMERIC,
            target_price NUMERIC,
            qty          INTEGER,
            xauusd_entry NUMERIC,
            usdinr_rate  NUMERIC,
            basis        NUMERIC,
            status       TEXT DEFAULT 'OPEN',
            pnl          NUMERIC,
            entry_time   TIMESTAMPTZ DEFAULT NOW(),
            exit_time    TIMESTAMPTZ,
            notes        TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS swing_levels (
            id          SERIAL PRIMARY KEY,
            xau_price   NUMERIC,
            mcx_equiv   NUMERIC,
            usdinr_rate NUMERIC,
            swing_type  TEXT,
            timestamp   TIMESTAMPTZ,
            touched     BOOLEAN DEFAULT FALSE,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id             SERIAL PRIMARY KEY,
            direction      TEXT,
            xauusd_price   NUMERIC,
            mcx_equiv      NUMERIC,
            usdinr_rate    NUMERIC,
            htf_bias       TEXT,
            swing_level_id INTEGER,
            entry_price    NUMERIC,
            stop_price     NUMERIC,
            target_price   NUMERIC,
            fired          BOOLEAN DEFAULT FALSE,
            created_at     TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    log.info("[DB] Tables initialised")


def get(key: str):
    try:
        cur = _get_conn().cursor()
        cur.execute("SELECT value FROM kv_store WHERE key=%s", (key,))
        row = cur.fetchone()
        return row["value"] if row else None
    except Exception as e:
        log.warning(f"[DB] get({key}) failed: {e}")
        return None


def set(key: str, value):
    try:
        cur = _get_conn().cursor()
        cur.execute("""
            INSERT INTO kv_store(key, value, updated_at)
            VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        """, (key, json.dumps(value)))
    except Exception as e:
        log.warning(f"[DB] set({key}) failed: {e}")


def save_trade(trade: dict) -> int:
    try:
        cur = _get_conn().cursor()
        cur.execute("""
            INSERT INTO trades
              (direction, entry_price, stop_price, target_price, qty,
               xauusd_entry, usdinr_rate, basis, notes)
            VALUES
              (%(direction)s, %(entry_price)s, %(stop_price)s, %(target_price)s,
               %(qty)s, %(xauusd_entry)s, %(usdinr_rate)s, %(basis)s, %(notes)s)
            RETURNING id
        """, trade)
        row = cur.fetchone()
        return row["id"]
    except Exception as e:
        log.error(f"[DB] save_trade failed: {e}")
        return -1


def close_trade(trade_id: int, pnl: float):
    try:
        cur = _get_conn().cursor()
        cur.execute("""
            UPDATE trades SET status='CLOSED', pnl=%s, exit_time=NOW()
            WHERE id=%s
        """, (pnl, trade_id))
    except Exception as e:
        log.error(f"[DB] close_trade failed: {e}")


def save_swing(swing: dict) -> int:
    try:
        cur = _get_conn().cursor()
        cur.execute("""
            INSERT INTO swing_levels (xau_price, mcx_equiv, usdinr_rate, swing_type, timestamp)
            VALUES (%(xau_price)s, %(mcx_equiv)s, %(usdinr_rate)s, %(swing_type)s, %(timestamp)s)
            RETURNING id
        """, swing)
        row = cur.fetchone()
        return row["id"]
    except Exception as e:
        log.error(f"[DB] save_swing failed: {e}")
        return -1


def get_active_swings(max_age_hours: int = 48):
    try:
        cur = _get_conn().cursor()
        cur.execute("""
            SELECT * FROM swing_levels
            WHERE touched = FALSE
              AND timestamp > NOW() - INTERVAL '1 hour' * %s
            ORDER BY timestamp DESC
            LIMIT 20
        """, (max_age_hours,))
        return list(cur.fetchall())
    except Exception as e:
        log.error(f"[DB] get_active_swings failed: {e}")
        return []


def mark_swing_touched(swing_id: int):
    try:
        cur = _get_conn().cursor()
        cur.execute("UPDATE swing_levels SET touched=TRUE WHERE id=%s", (swing_id,))
    except Exception as e:
        log.error(f"[DB] mark_swing_touched failed: {e}")


def save_signal(signal: dict):
    try:
        cur = _get_conn().cursor()
        cur.execute("""
            INSERT INTO signals
              (direction, xauusd_price, mcx_equiv, usdinr_rate, htf_bias,
               swing_level_id, entry_price, stop_price, target_price)
            VALUES
              (%(direction)s, %(xauusd_price)s, %(mcx_equiv)s, %(usdinr_rate)s,
               %(htf_bias)s, %(swing_level_id)s, %(entry_price)s,
               %(stop_price)s, %(target_price)s)
        """, signal)
    except Exception as e:
        log.error(f"[DB] save_signal failed: {e}")


def get_recent_trades(limit: int = 50):
    try:
        cur = _get_conn().cursor()
        cur.execute("""
            SELECT * FROM trades ORDER BY entry_time DESC LIMIT %s
        """, (limit,))
        return list(cur.fetchall())
    except Exception as e:
        log.error(f"[DB] get_recent_trades failed: {e}")
        return []
