"""
upstox_auth.py — Upstox OAuth 2.0 for GoldEngine
Same pattern as StructureEngine — /login redirects to Upstox, /callback stores token.
"""
import os
import requests
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse
from config import CONFIG
import db

log = logging.getLogger(__name__)

UPSTOX_AUTH_URL  = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


def _api_key() -> str:
    return os.getenv("UPSTOX_API_KEY", CONFIG.get("upstox_api_key", "")).strip()

def _api_secret() -> str:
    return os.getenv("UPSTOX_API_SECRET", "").strip()

def _redirect_uri() -> str:
    configured = os.getenv("UPSTOX_REDIRECT_URI", "").strip()
    if configured:
        return configured
    return "http://localhost:5000/callback"

def get_login_url() -> str:
    params = {
        "response_type": "code",
        "client_id":     _api_key(),
        "redirect_uri":  _redirect_uri(),
    }
    return f"{UPSTOX_AUTH_URL}?{urlencode(params)}"

def exchange_code_for_token(auth_code: str) -> str | None:
    try:
        r = requests.post(
            UPSTOX_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            data={
                "code":          auth_code,
                "client_id":     _api_key(),
                "client_secret": _api_secret(),
                "redirect_uri":  _redirect_uri(),
                "grant_type":    "authorization_code",
            },
            timeout=15,
        )
        if r.status_code != 200:
            log.error(f"[Auth] Token exchange failed: {r.status_code} — {r.text[:200]}")
            return None
        return r.json().get("access_token")
    except Exception as e:
        log.error(f"[Auth] Token exchange error: {e}")
        return None

def save_token(token: str):
    CONFIG["upstox_access_token"] = token
    db.set("upstox_access_token", {"token": token, "saved_at": datetime.now().isoformat()})
    log.info("[Auth] Token saved")

def load_token_from_db() -> str | None:
    try:
        data = db.get("upstox_access_token")
        if not data or not data.get("token"):
            return None
        saved_at  = datetime.fromisoformat(data["saved_at"])
        now_utc   = datetime.utcnow()
        if saved_at.tzinfo is not None:
            saved_at = saved_at.replace(tzinfo=None)
        cutoff = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
        if now_utc < cutoff:
            cutoff -= timedelta(days=1)
        if saved_at < cutoff:
            return None
        return data["token"]
    except Exception as e:
        log.warning(f"[Auth] load_token failed: {e}")
        return None

def restore_token_on_startup():
    token = load_token_from_db()
    if token:
        CONFIG["upstox_access_token"] = token
        log.info("[Auth] Token restored from DB")
        return True
    CONFIG["upstox_access_token"] = ""
    log.warning("[Auth] No valid token — visit /login")
    return False
