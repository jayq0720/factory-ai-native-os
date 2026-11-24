"""轻量级零依赖滑动窗口 API 限流器：防止脚本死循环与 API Key 暴力猜解。"""

from __future__ import annotations

import threading
from collections import deque
from time import monotonic

from fos.governance.apikeys import parse as parse_apikey


class SlidingWindowLimiter:
    """按调用方（API Key ID 或来源 IP）统计滑动窗口请求次数。limit<=0 表示关闭限流。"""

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit = int(limit)
        self.window_s = float(window_s)
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = {}

    def check(self, key: str, now: float | None = None) -> tuple[bool, int, int]:
        """返回 (allowed, remaining, retry_after_seconds)。"""
        if self.limit <= 0:
            return True, 0, 0
        ts = monotonic() if now is None else now
        cutoff = ts - self.window_s
        with self._lock:
            q = self._buckets.get(key)
            if q is None:
                q = deque()
                self._buckets[key] = q
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.limit:
                retry = max(1, int(q[0] + self.window_s - ts) + 1)
                return False, 0, retry
            q.append(ts)
            remaining = max(0, self.limit - len(q))
            return True, remaining, 0


def client_bucket_key(auth_header: str | None, api_key_header: str | None, client_host: str | None) -> str:
    """提取限流分桶标识：合法格式 API Key 按 key_id 分桶，其余按来源 IP 分桶。"""
    token = ""
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    elif api_key_header:
        token = api_key_header.strip()
    parsed = parse_apikey(token) if token else None
    if parsed is not None:
        return f"key:{parsed[0]}"
    return f"ip:{client_host or 'unknown'}"
