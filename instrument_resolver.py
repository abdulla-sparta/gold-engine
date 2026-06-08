"""
instrument_resolver.py — Resolves Upstox instrument keys at startup
Uses the search API so we never hardcode expiry-dependent keys.
"""
import logging
import requests
from config import CONFIG

log = logging.getLogger(__name__)
BASE = "https://api.upstox.com/v2"


def _headers():
    return {
        "Authorization": f"Bearer {CONFIG.get('upstox_access_token', '')}",
        "Accept": "application/json",
    }


def search_instrument(query: str, exchanges: str = "ALL", segments: str = "ALL",
                      expiry: str = None, instrument_types: str = None) -> list:
    params = {
        "query":       query,
        "exchanges":   exchanges,
        "segments":    segments,
        "page_number": 1,
        "records":     10,
    }
    if expiry:
        params["expiry"] = expiry
    if instrument_types:
        params["instrument_types"] = instrument_types
    try:
        r = requests.get(f"{BASE}/instruments/search", headers=_headers(),
                         params=params, timeout=10)
        if r.status_code != 200:
            log.warning(f"[InstrumentResolver] search failed {r.status_code}: {r.text[:200]}")
            return []
        return r.json().get("data", [])
    except Exception as e:
        log.warning(f"[InstrumentResolver] search error: {e}")
        return []


def resolve_goldten() -> str:
    """Find current-month GOLDTEN MCX futures instrument key."""
    results = search_instrument(
        query="GOLDTEN",
        exchanges="MCX",
        segments="COMM",
        expiry="current_month",
    )
    # Filter to FUT only
    futs = [r for r in results if r.get("instrument_type") == "FUT"]
    if not futs:
        # Try without expiry filter
        results = search_instrument(query="GOLDTEN", exchanges="MCX")
        futs = [r for r in results if r.get("instrument_type") == "FUT"]
    if futs:
        key = futs[0]["instrument_key"]
        sym = futs[0].get("trading_symbol", "")
        log.info(f"[InstrumentResolver] GOLDTEN → {key} ({sym})")
        CONFIG["goldten_instrument_key"] = key
        CONFIG["goldten_trading_symbol"] = sym
        return key
    log.error("[InstrumentResolver] Could not resolve GOLDTEN instrument key!")
    return CONFIG.get("goldten_instrument_key", "")


def resolve_usdinr() -> str:
    """Find current-month USDINR NSE currency futures instrument key."""
    results = search_instrument(
        query="USDINR",
        exchanges="NSE",
        segments="CURR",
        expiry="current_month",
    )
    futs = [r for r in results if r.get("instrument_type") == "FUT"]
    if not futs:
        results = search_instrument(query="USDINR", exchanges="NSE", segments="CURR")
        futs = [r for r in results if r.get("instrument_type") == "FUT"]
    if futs:
        key = futs[0]["instrument_key"]
        sym = futs[0].get("trading_symbol", "")
        log.info(f"[InstrumentResolver] USDINR → {key} ({sym})")
        CONFIG["usdinr_instrument_key"] = key
        return key
    log.error("[InstrumentResolver] Could not resolve USDINR instrument key!")
    return CONFIG.get("usdinr_instrument_key", "")


def resolve_all():
    """Call at startup after OAuth token is available."""
    log.info("[InstrumentResolver] Resolving instrument keys...")
    resolve_goldten()
    resolve_usdinr()
