"""原件存储。按 SHA-256 内容寻址，同一文件只存一份；支持本地目录与 S3/阿里云 OSS 兼容后端。"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote, urlparse

import httpx

if TYPE_CHECKING:
    from fos.config import Settings

_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class BlobStore(Protocol):
    def put(self, tenant_id: str, data: bytes) -> str: ...
    def get(self, tenant_id: str, digest: str) -> bytes: ...
    def uri(self, tenant_id: str, digest: str) -> str: ...


def _validate_ids(tenant_id: str, digest: str) -> None:
    if not _TENANT_RE.match(tenant_id):
        raise ValueError(f"非法租户标识：{tenant_id!r}")
    if not _DIGEST_RE.match(digest):
        raise ValueError(f"非法内容摘要：{digest!r}")


class LocalBlobStore:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, tenant_id: str, digest: str) -> Path:
        _validate_ids(tenant_id, digest)
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


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign_s3_v4(
    *,
    method: str,
    url: str,
    region: str,
    access_key: str,
    secret_key: str,
    payload_sha256: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """生成标准 AWS SigV4 请求头（兼容阿里云 OSS S3 协议、MinIO 与 AWS S3）。"""
    dt = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date = dt.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = dt.strftime("%Y%m%d")
    parsed = urlparse(url)
    host = parsed.netloc
    canonical_uri = quote(parsed.path or "/", safe="/-_.~")
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_sha256}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_req = "\n".join(
        [method.upper(), canonical_uri, parsed.query, canonical_headers, signed_headers, payload_sha256]
    )
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    req_hash = hashlib.sha256(canonical_req.encode("utf-8")).hexdigest()
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{req_hash}"
    k_date = _hmac_sha256(f"AWS4{secret_key}".encode(), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, "s3")
    k_signing = _hmac_sha256(k_service, "aws4_request")
    sig = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={sig}"
    )
    return {
        "Host": host,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
        "Authorization": auth,
    }


class S3BlobStore:
    """S3 / 阿里云 OSS 兼容内容寻址原件存储（零额外 SDK 依赖，支持 K8s 多副本共享）。"""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        prefix: str = "blobs/",
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not endpoint or not bucket:
            raise ValueError("S3BlobStore 需要配置 s3_endpoint 与 s3_bucket")
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket.strip("/")
        self.region = region or "cn-shanghai"
        self.access_key = access_key
        self.secret_key = secret_key
        self.prefix = prefix.strip("/")
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def _key(self, tenant_id: str, digest: str) -> str:
        _validate_ids(tenant_id, digest)
        tail = f"{tenant_id}/{digest[:2]}/{digest}"
        return f"{self.prefix}/{tail}" if self.prefix else tail

    def _url(self, key: str) -> str:
        return f"{self.endpoint}/{self.bucket}/{key}"

    def _headers(self, method: str, url: str, payload_sha256: str) -> dict[str, str]:
        return sign_s3_v4(
            method=method,
            url=url,
            region=self.region,
            access_key=self.access_key,
            secret_key=self.secret_key,
            payload_sha256=payload_sha256,
        )

    def put(self, tenant_id: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        key = self._key(tenant_id, digest)
        url = self._url(key)
        head = self._client.head(url, headers=self._headers("HEAD", url, _EMPTY_SHA256))
        if head.status_code == 200:
            return digest
        resp = self._client.put(url, content=data, headers=self._headers("PUT", url, digest))
        resp.raise_for_status()
        return digest

    def get(self, tenant_id: str, digest: str) -> bytes:
        key = self._key(tenant_id, digest)
        url = self._url(key)
        resp = self._client.get(url, headers=self._headers("GET", url, _EMPTY_SHA256))
        if resp.status_code == 404:
            raise FileNotFoundError(f"对象不存在：{self.uri(tenant_id, digest)}")
        resp.raise_for_status()
        return resp.content

    def uri(self, tenant_id: str, digest: str) -> str:
        key = self._key(tenant_id, digest)
        return f"s3://{self.bucket}/{key}"


def build_blob_store(settings: Settings) -> BlobStore:
    """按配置构造原件存储：默认本地目录，FOS_BLOB_BACKEND=s3 时启用 S3/阿里云 OSS。"""
    if settings.blob_backend == "s3":
        return S3BlobStore(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            prefix=settings.s3_prefix,
        )
    return LocalBlobStore(settings.blob_dir)
