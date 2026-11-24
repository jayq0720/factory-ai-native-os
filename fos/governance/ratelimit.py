"""轻量级滑动窗口 API 限流器：支持进程内与多副本 Redis 共享后端，防止暴力猜解与脚本死循环。"""

from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from time import monotonic
from typing import Protocol
from urllib.parse import urlparse

from fos.governance.apikeys import parse as parse_apikey

log = logging.getLogger(__name__)


class RateLimiter(Protocol):
    limit: int

    def check(self, key: str, now: float | None = None) -> tuple[bool, int, int]: ...


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


_LUA_SLIDING = (
    "local k=KEYS[1] local now=tonumber(ARGV[1]) local win=tonumber(ARGV[2]) "
    "local lim=tonumber(ARGV[3]) local mem=ARGV[4] "
    "redis.call('ZREMRANGEBYSCORE',k,'-inf',now-win) "
    "local cnt=redis.call('ZCARD',k) "
    "if cnt>=lim then "
    "local first=redis.call('ZRANGE',k,0,0,'WITHSCORES') "
    "local retry=1 if #first>=2 then retry=math.max(1,math.ceil((tonumber(first[2])+win-now)/1000)) end "
    "return {0,0,retry} end "
    "redis.call('ZADD',k,now,mem) redis.call('PEXPIRE',k,win) "
    "return {1,lim-cnt-1,0}"
)


def _resp_encode(*parts: str) -> bytes:
    buf = [f"*{len(parts)}\r\n".encode()]
    for p in parts:
        raw = p.encode("utf-8")
        buf.append(f"${len(raw)}\r\n".encode() + raw + b"\r\n")
    return b"".join(buf)


def _resp_read_line(f) -> bytes:
    line = f.readline()
    if not line:
        raise OSError("Redis 连接中断")
    return line.rstrip(b"\r\n")


def _resp_parse(f):
    line = _resp_read_line(f)
    prefix, body = line[:1], line[1:]
    if prefix == b"+":
        return body.decode("utf-8")
    if prefix == b"-":
        raise RuntimeError(f"Redis 错误：{body.decode('utf-8', 'replace')}")
    if prefix == b":":
        return int(body)
    if prefix == b"$":
        n = int(body)
        if n < 0:
            return None
        data = f.read(n)
        f.read(2)
        return data.decode("utf-8")
    if prefix == b"*":
        n = int(body)
        return [_resp_parse(f) for _ in range(n)]
    raise RuntimeError(f"不支持的 RESP 响应：{line!r}")


def _redis_eval_sliding(
    redis_url: str, key: str, now_ms: int, win_ms: int, limit: int, timeout_s: float
) -> tuple[bool, int, int]:
    u = urlparse(redis_url)
    host = u.hostname or "127.0.0.1"
    port = u.port or 6379
    db = int(u.path.lstrip("/") or "0")
    member = f"{now_ms}-{uuid.uuid4().hex[:8]}"
    with socket.create_connection((host, port), timeout=timeout_s) as sock:
        sock.settimeout(timeout_s)
        f = sock.makefile("rb")
        if u.password:
            auth_args = ("AUTH", u.username, u.password) if u.username else ("AUTH", u.password)
            sock.sendall(_resp_encode(*auth_args))
            _resp_parse(f)
        if db > 0:
            sock.sendall(_resp_encode("SELECT", str(db)))
            _resp_parse(f)
        eval_pkt = _resp_encode("EVAL", _LUA_SLIDING, "1", key, str(now_ms), str(win_ms), str(limit), member)
        sock.sendall(eval_pkt)
        res = _resp_parse(f)
        return bool(res[0]), int(res[1]), int(res[2])


class RedisSlidingWindowLimiter:
    """多副本共享 Redis 滑动窗口限流器（零额外依赖；Redis 抖动时自动降级本地限流）。"""

    def __init__(
        self,
        limit: int,
        redis_url: str,
        *,
        window_s: float = 60.0,
        key_prefix: str = "fos:rl:",
        timeout_s: float = 0.5,
        eval_fn: Callable[[str, int, int, int], tuple[bool, int, int]] | None = None,
    ) -> None:
        self.limit = int(limit)
        self.redis_url = redis_url
        self.window_s = float(window_s)
        self.key_prefix = key_prefix
        self.timeout_s = float(timeout_s)
        self._eval_fn = eval_fn
        self._fallback = SlidingWindowLimiter(limit, window_s=window_s)

    def check(self, key: str, now: float | None = None) -> tuple[bool, int, int]:
        if self.limit <= 0:
            return True, 0, 0
        now_ms = int((time.time() if now is None else now) * 1000)
        win_ms = int(self.window_s * 1000)
        rkey = f"{self.key_prefix}{key}"
        try:
            if self._eval_fn is not None:
                return self._eval_fn(rkey, now_ms, win_ms, self.limit)
            return _redis_eval_sliding(self.redis_url, rkey, now_ms, win_ms, self.limit, self.timeout_s)
        except Exception as e:
            log.warning("Redis 限流后端不可用，自动降级至本地滑动窗口：%s", e)
            return self._fallback.check(key, now=now)


def build_rate_limiter(limit: int, redis_url: str = "", *, window_s: float = 60.0) -> RateLimiter:
    """按配置构造限流器：提供 redis_url 时启用多副本 Redis 限流，否则使用本地内存窗口。"""
    if redis_url.strip():
        return RedisSlidingWindowLimiter(limit, redis_url.strip(), window_s=window_s)
    return SlidingWindowLimiter(limit, window_s=window_s)


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
