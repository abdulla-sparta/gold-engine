"""
instrument_resolver.py — Resolves Upstox instrument keys at startup.
Uses the search API so we never hardcode expiry-dependent keys.

Fix (UDAPI1026): Added current_month → next_month → bare fallback retry chain,
strict empty-string return (never falls back to a stale CONFIG value), and
a module-level re-resolve helper used by place_order() as a last resort.
"""
import logging
import requests
from config import CONFIG

log = logging.getLogger(__name__)
BASE = "https://api.upstox.com/v2"


def _headers() -> dict:
    token = CONFIG.get("upstox_access_token", "")
    if not token:
        log.warning("[InstrumentResolver] upstox_access_token is empty — API calls will fail")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def search_instrument(
    query: str,
    exchanges: str = "ALL",
    segments: str = "ALL",
    expiry: str = None,
    instrument_types: str = None,
) -> list:
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
        r = requests.get(
            f"{BASE}/instruments/search",
            headers=_headers(),
            params=params,
            timeout=10,
        )
        if r.status_code == 401:
            log.error("[InstrumentResolver] 401 Unauthorized — access token expired, re-auth needed")
            return []
        if r.status_code != 200:
            log.warning(
                f"[InstrumentResolver] search failed {r.status_code}: {r.text[:300]}"
            )
            return []
        data = r.json().get("data", [])
        log.debug(f"[InstrumentResolver] search '{query}' expiry={expiry} → {len(data)} results")
        return data
    except requests.exceptions.Timeout:
        log.warning(f"[InstrumentResolver] search timeout for query='{query}'")
        return []
    except Exception as e:
        log.warning(f"[InstrumentResolver] search error: {e}")
        return []


def resolve_goldten() -> str:
    """
    Find the active GOLDTEN MCX futures instrument key.

    Retry chain:
      1. expiry=current_month  (normal case)
      2. expiry=next_month     (near rollover — last week of expiry month)
      3. no expiry filter      (bare search, picks first FUT returned)

    Returns the instrument_key string on success, or "" on failure.
    Never falls back to a stale CONFIG value — callers must treat "" as an error.
    """
    attempts = [
        ("current_month", "MCX", "COMM"),
        ("next_month",    "MCX", "COMM"),
        (None,            "MCX", "COMM"),
        (None,            "ALL", "ALL"),   # last resort: any exchange
    ]

    for expiry, exchanges, segments in attempts:
        results = search_instrument(
            query="GOLDTEN",
            exchanges=exchanges,
            segments=segments,
            expiry=expiry,
        )
        futs = [r for r in results if r.get("instrument_type") == "FUT"]
        if futs:
            # Prefer the nearest expiry — sort by trading_symbol (GOLDTEN26JUNFUT < GOLDTEN26JULFUT)
            futs.sort(key=lambda x: x.get("trading_symbol", ""))
            key = futs[0]["instrument_key"]
            sym = futs[0].get("trading_symbol", "")
            log.info(
                f"[InstrumentResolver] GOLDTEN resolved → {key} ({sym})"
                f"  [attempt: expiry={expiry}, exchanges={exchanges}]"
            )
            CONFIG["goldten_instrument_key"] = key
            CONFIG["goldten_trading_symbol"] = sym
            return key

    log.error(
        "[InstrumentResolver] GOLDTEN resolution FAILED — all attempts returned no FUT. "
        "Check: (1) access token valid? (2) MCX market data subscription active?"
    )
    # Do NOT write "" into CONFIG here — keep the last successfully resolved key alive
    # so the dashboard can still display it, but return "" so callers know resolution failed.
    return ""


def resolve_usdinr() -> str:
    """
    Find the active USDINR NSE currency futures instrument key.

    Retry chain: current_month → next_month → bare search.
    Returns instrument_key string or "".
    """
    attempts = [
        ("current_month", "NSE", "CURR"),
        ("next_month",    "NSE", "CURR"),
        (None,            "NSE", "CURR"),
        (None,            "ALL", "ALL"),
    ]

    for expiry, exchanges, segments in attempts:
        results = search_instrument(
            query="USDINR",
            exchanges=exchanges,
            segments=segments,
            expiry=expiry,
        )
        futs = [r for r in results if r.get("instrument_type") == "FUT"]
        if futs:
            futs.sort(key=lambda x: x.get("trading_symbol", ""))
            key = futs[0]["instrument_key"]
            sym = futs[0].get("trading_symbol", "")
            log.info(
                f"[InstrumentResolver] USDINR resolved → {key} ({sym})"
                f"  [attempt: expiry={expiry}, exchanges={exchanges}]"
            )
            CONFIG["usdinr_instrument_key"] = key
            return key

    log.error("[InstrumentResolver] USDINR resolution FAILED — all attempts returned no FUT.")
    return ""


def ensure_goldten_key() -> str:
    """
    Return the current GOLDTEN instrument key from CONFIG, or attempt a live
    re-resolve if it's missing/empty.  Used by place_order() as a last resort.

    Returns the key string, or "" if resolution fails.
    """
    key = CONFIG.get("goldten_instrument_key", "")
    if key:
        return key
    log.warning("[InstrumentResolver] ensure_goldten_key: CONFIG key empty — attempting live re-resolve")
    return resolve_goldten()


def resolve_all():
    """
    Resolve all instrument keys.  Call at startup after OAuth token is available.
    Logs a prominent warning if GOLDTEN resolution fails so it's impossible to miss.
    """
    log.info("[InstrumentResolver] Resolving instrument keys...")

    goldten_key = resolve_goldten()
    if not goldten_key:
        log.error(
            "[InstrumentResolver] ⚠️  GOLDTEN instrument key UNRESOLVED.\n"
            "  → Live orders will be blocked until this is resolved.\n"
            "  → Re-Auth via the dashboard and restart the engine to retry."
        )
    else:
        log.info(f"[InstrumentResolver] ✅ GOLDTEN ready: {goldten_key} ({CONFIG.get('goldten_trading_symbol','')})")

    usdinr_key = resolve_usdinr()
    if not usdinr_key:
        log.warning("[InstrumentResolver] ⚠️  USDINR instrument key unresolved — conversion may use stale rate.")
    else:
        log.info(f"[InstrumentResolver] ✅ USDINR ready: {usdinr_key}")
