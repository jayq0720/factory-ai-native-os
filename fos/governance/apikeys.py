"""API Key：fos_<key_id>_<secret>。库里只存 secret 的 SHA-256。

secret 是 32 字节随机数，熵足够高，所以用 SHA-256 而不是慢哈希；
比较用常量时间。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ApiKey, User, utcnow

PREFIX = "fos"


@dataclass(frozen=True)
class IssuedKey:
    key_id: str
    plaintext: str  # 只在创建时返回一次


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def parse(plaintext: str) -> tuple[str, str] | None:
    parts = plaintext.split("_")
    if len(parts) != 3 or parts[0] != PREFIX or len(parts[1]) != 16 or not parts[2]:
        return None
    return parts[1], parts[2]


async def issue(session: AsyncSession, user: User, label: str = "") -> IssuedKey:
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32).replace("_", "-")
    session.add(
        ApiKey(id=key_id, tenant_id=user.tenant_id, user_id=user.id, secret_sha256=_hash(secret), label=label)
    )
    await session.flush()
    return IssuedKey(key_id=key_id, plaintext=f"{PREFIX}_{key_id}_{secret}")


async def verify(session: AsyncSession, plaintext: str) -> tuple[ApiKey, User] | None:
    parsed = parse(plaintext)
    if not parsed:
        return None
    key_id, secret = parsed
    row = (
        await session.execute(
            select(ApiKey, User).join(User, User.id == ApiKey.user_id).where(ApiKey.id == key_id)
        )
    ).first()
    if not row:
        return None
    key, user = row
    if key.revoked_at is not None or not user.is_active or key.tenant_id != user.tenant_id:
        return None
    if not hmac.compare_digest(key.secret_sha256, _hash(secret)):
        return None
    key.last_used_at = utcnow()
    return key, user
