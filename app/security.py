"""Authentication helpers for browser cookies and API bearer tokens."""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any, Dict, Optional

SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
SESSION_COOKIE = "codelib_session"


def issue_session(store, user: Dict[str, Any]) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    store.create_auth_session(user["id"], token_hash, time.time() + SESSION_TTL_SECONDS)
    return token


def session_user(store, request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(SESSION_COOKIE, "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return store.user_for_session(token_hash)


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()
