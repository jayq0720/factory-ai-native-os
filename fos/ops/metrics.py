"""生产可观测性（零依赖 Prometheus OpenMetrics 指标）与不可变账本审计流水查询。"""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Event


def _esc(val: str) -> str:
    return val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class HttpMetrics:
    """线程安全的轻量级 HTTP 请求计数与耗时累积器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str], int] = {}
        self._dur_sum: float = 0.0
        self._dur_count: int = 0

    def record(self, method: str, status_code: int, duration_s: float) -> None:
        cls = f"{status_code // 100}xx"
        key = (method.upper(), cls)
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            self._dur_sum += max(0.0, float(duration_s))
            self._dur_count += 1

    def render_lines(self) -> list[str]:
        with self._lock:
            items = sorted(self._counts.items())
            d_sum = self._dur_sum
            d_cnt = self._dur_count
        lines = [
            "# HELP fos_http_requests_total Total HTTP requests handled by status class.",
            "# TYPE fos_http_requests_total counter",
        ]
        for (method, cls), cnt in items:
            lines.append(f'fos_http_requests_total{{method="{_esc(method)}",status_class="{cls}"}} {cnt}')
        lines.extend(
            [
                "# HELP fos_http_request_duration_seconds_sum Total HTTP request duration in seconds.",
                "# TYPE fos_http_request_duration_seconds_sum counter",
                f"fos_http_request_duration_seconds_sum {d_sum:.6f}",
                "# HELP fos_http_request_duration_seconds_count Total timed HTTP requests.",
                "# TYPE fos_http_request_duration_seconds_count counter",
                f"fos_http_request_duration_seconds_count {d_cnt}",
            ]
        )
        return lines


def render_prometheus(http_metrics: HttpMetrics, overview: dict[str, Any]) -> str:
    """将 HTTP 指标与多租户 SaaS 运行快照渲染为标准 Prometheus 文本格式。"""
    lines = http_metrics.render_lines()
    tenants = overview.get("tenants") or []
    lines.extend(
        [
            "# HELP fos_tenants_total Number of registered factory tenants.",
            "# TYPE fos_tenants_total gauge",
            f"fos_tenants_total {int(overview.get('tenant_count', len(tenants)))}",
            "# HELP fos_events_total Total ledger events per tenant.",
            "# TYPE fos_events_total gauge",
        ]
    )
    for t in tenants:
        tid = _esc(str(t["tenant_id"]))
        lines.append(f'fos_events_total{{tenant="{tid}"}} {int(t["counts"]["events_total"])}')
    lines.extend(
        [
            "# HELP fos_open_todos Open todo items per tenant.",
            "# TYPE fos_open_todos gauge",
        ]
    )
    for t in tenants:
        tid = _esc(str(t["tenant_id"]))
        lines.append(f'fos_open_todos{{tenant="{tid}"}} {int(t["counts"]["open_todos"])}')
    lines.extend(
        [
            "# HELP fos_pending_approvals Pending approvals per tenant.",
            "# TYPE fos_pending_approvals gauge",
        ]
    )
    for t in tenants:
        tid = _esc(str(t["tenant_id"]))
        lines.append(f'fos_pending_approvals{{tenant="{tid}"}} {int(t["counts"]["pending_approvals"])}')
    lines.extend(
        [
            "# HELP fos_llm_tokens_7d LLM tokens consumed in the last 7 days per tenant.",
            "# TYPE fos_llm_tokens_7d gauge",
        ]
    )
    for t in tenants:
        tid = _esc(str(t["tenant_id"]))
        lines.append(f'fos_llm_tokens_7d{{tenant="{tid}"}} {int(t["counts"]["llm_tokens_7d"])}')
    lines.extend(
        [
            "# HELP fos_security_baseline_ok Security baseline status (1=pass, 0=fail) per tenant.",
            "# TYPE fos_security_baseline_ok gauge",
        ]
    )
    for t in tenants:
        tid = _esc(str(t["tenant_id"]))
        ok = 1 if t.get("security_passed") else 0
        lines.append(f'fos_security_baseline_ok{{tenant="{tid}"}} {ok}')
    return "\n".join(lines) + "\n"


async def query_audit_log(
    session: AsyncSession,
    tenant_id: str,
    *,
    event_prefix: str | None = None,
    object_type: str | None = None,
    actor_id: str | None = None,
    data_mode: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """按租户分页查询不可变事件账本审计流水（倒序，含证据链计数）。"""
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    conds: list[Any] = [Event.tenant_id == tenant_id]
    if event_prefix:
        conds.append(Event.type.startswith(event_prefix))
    if object_type:
        conds.append(Event.object_type == object_type)
    if actor_id:
        conds.append(Event.actor_id == actor_id)
    if data_mode:
        conds.append(Event.data_mode == data_mode)

    total = int((await session.scalar(select(func.count()).select_from(Event).where(*conds))) or 0)
    rows = (
        await session.scalars(select(Event).where(*conds).order_by(Event.seq.desc()).offset(off).limit(lim))
    ).all()
    items = [
        {
            "seq": int(e.seq),
            "id": e.id,
            "type": e.type,
            "object_type": e.object_type,
            "object_id": e.object_id,
            "actor_kind": e.actor_kind,
            "actor_id": e.actor_id,
            "actor_name": e.actor_name,
            "data_mode": e.data_mode,
            "evidence_count": len(e.evidence or []),
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
        }
        for e in rows
    ]
    return {"tenant_id": tenant_id, "total": total, "limit": lim, "offset": off, "items": items}
