"""原件存储。按 SHA-256 内容寻址，同一文件只存一份。本地目录实现；生产换阿里云 OSS（同一接口）。"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path

_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class LocalBlobStore:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, tenant_id: str, digest: str) -> Path:
        if not _TENANT_RE.match(tenant_id):
            raise ValueError(f"非法租户标识：{tenant_id!r}")
        if not _DIGEST_RE.match(digest):
            raise ValueError(f"非法内容摘要：{digest!r}")
        return self.root / tenant_id / digest[:2] / digest

    def put(self, tenant_id: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(tenant_id, digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f".{digest}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        return digest

    def get(self, tenant_id: str, digest: str) -> bytes:
        return self._path(tenant_id, digest).read_bytes()

    def uri(self, tenant_id: str, digest: str) -> str:
        self._path(tenant_id, digest)
        return f"blob://{tenant_id}/{digest}"
