"""原件存储。按 SHA-256 内容寻址，同一文件只存一份。本地目录实现；生产换阿里云 OSS（同一接口）。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class LocalBlobStore:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, tenant_id: str, digest: str) -> Path:
        return self.root / tenant_id / digest[:2] / digest

    def put(self, tenant_id: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(tenant_id, digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return digest

    def get(self, tenant_id: str, digest: str) -> bytes:
        return self._path(tenant_id, digest).read_bytes()

    def uri(self, tenant_id: str, digest: str) -> str:
        return f"blob://{tenant_id}/{digest}"
