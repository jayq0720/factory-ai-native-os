"""Web 工作台无状态签名会话 Cookie（HMAC-SHA256，HttpOnly，支持撤销联查）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

COOKIE_NAME = "fos_session"
_EPHEMERAL_SECRET = secrets.token_hex(32)


def resolve_secret(configured: str) -> str:
    return configured.strip() or _EPHEMERAL_SECRET


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def sign_session(
    *,
    user_id: str,
    tenant_id: str,
    key_id: str,
    secret: str,
    ttl_hours: int = 12,
    now: int | None = None,
) -> str:
    ts = int(time.time()) if now is None else int(now)
    payload = {"uid": user_id, "tid": tenant_id, "kid": key_id, "exp": ts + max(1, ttl_hours) * 3600}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_session(token: str, *, secret: str, now: int | None = None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    body, _, sig_b64 = token.partition(".")
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    try:
        got = _b64d(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, got):
        return None
    try:
        data = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    ts = int(time.time()) if now is None else int(now)
    if int(data.get("exp", 0)) <= ts:
        return None
    if not data.get("uid") or not data.get("tid") or not data.get("kid"):
        return None
    return data
