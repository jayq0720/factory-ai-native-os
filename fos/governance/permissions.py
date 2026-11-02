"""权限串与校验。

格式：<域>.<对象>:<动作>，例如 "eng.project:write"、"warehouse.stock:read"。
通配：域或对象可用 "*"，例如 "eng.*:read"、"*.*:*"（只给 admin）。
动作：read / write / approve / admin。
"""

from __future__ import annotations

import re

ACTIONS = ("read", "write", "approve", "admin")
_PERM = re.compile(r"^(\*|[a-z][a-z0-9_]*)\.(\*|[a-z][a-z0-9_]*):(\*|read|write|approve|admin)$")


def is_valid_permission(perm: str) -> bool:
    return bool(_PERM.match(perm))


def _split(perm: str) -> tuple[str, str, str]:
    m = _PERM.match(perm)
    if not m:
        raise ValueError(f"非法权限串: {perm!r}")
    return m.group(1), m.group(2), m.group(3)


def grants(granted: frozenset[str] | set[str], required: str) -> bool:
    """granted 中是否有一条覆盖 required。required 不允许带通配。"""
    rd, ro, ra = _split(required)
    if "*" in (rd, ro, ra):
        raise ValueError("required 不能带通配")
    for g in granted:
        gd, go, ga = _split(g)
        if gd in ("*", rd) and go in ("*", ro) and ga in ("*", ra):
            return True
    return False
