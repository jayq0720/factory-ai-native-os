"""不可变事件账本实时结构一致性、证据链覆盖率与投影对齐审计（只读）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Event, ObjectState, TenantCounter


async def verify_tenant_ledger(session: AsyncSession, tenant_id: str) -> dict[str, Any]:
    """实时只读审计单租户账本：序号连续性、计数器对齐、真实事件证据链覆盖与投影对象同步。"""
    q_ev = (
        select(Event.seq, Event.object_type, Event.object_id, Event.data_mode, Event.evidence)
        .where(Event.tenant_id == tenant_id)
        .order_by(Event.seq.asc())
    )
    rows = (await session.execute(q_ev)).all()

    total_events = len(rows)
    gaps_count = 0
    first_gap: int | None = None
    expected_seq = 1
    distinct_objs: set[tuple[str, str]] = set()
    real_total = 0
    real_with_evidence = 0

    for seq, obj_type, obj_id, data_mode, evidence in rows:
        seq_i = int(seq)
        if seq_i != expected_seq:
            gaps_count += 1
            if first_gap is None:
                first_gap = expected_seq
            expected_seq = seq_i + 1
        else:
            expected_seq += 1
        distinct_objs.add((str(obj_type), str(obj_id)))
        if data_mode == "real":
            real_total += 1
            if evidence:
                real_with_evidence += 1

    last_seq = int(rows[-1][0]) if rows else 0
    ctr = await session.get(TenantCounter, tenant_id)
    counter_seq = int(ctr.last_seq) if ctr is not None else 0

    seq_monotonic_ok = gaps_count == 0
    counter_aligned = counter_seq == last_seq
    evidence_pct = round(real_with_evidence / real_total * 100.0, 2) if real_total else 100.0

    q_proj = select(func.count(ObjectState.object_id)).where(ObjectState.tenant_id == tenant_id)
    projected_cnt = int((await session.execute(q_proj)).scalar_one() or 0)
    event_objs_cnt = len(distinct_objs)
    projection_ok = projected_cnt >= event_objs_cnt

    checks = [
        {
            "name": "seq_monotonic",
            "ok": seq_monotonic_ok,
            "detail": (
                f"序号 1..{last_seq} 连续无断层"
                if seq_monotonic_ok
                else f"发现 {gaps_count} 处序号断层/异常（首个缺失序号 {first_gap}）"
            ),
        },
        {
            "name": "counter_aligned",
            "ok": counter_aligned,
            "detail": (
                f"TenantCounter.last_seq={counter_seq} 与账本最新序号一致"
                if counter_aligned
                else f"计数器漂移：TenantCounter.last_seq={counter_seq} != 账本 {last_seq}"
            ),
        },
        {
            "name": "evidence_coverage",
            "ok": evidence_pct == 100.0,
            "detail": f"真实事件证据链覆盖 {real_with_evidence}/{real_total}（{evidence_pct}%）",
        },
        {
            "name": "projection_sync",
            "ok": projection_ok,
            "detail": f"投影对象 {projected_cnt} 个 / 账本涉及对象 {event_objs_cnt} 个",
        },
    ]

    ok = seq_monotonic_ok and counter_aligned and projection_ok
    return {
        "tenant_id": tenant_id,
        "ok": ok,
        "events_total": total_events,
        "last_seq": last_seq,
        "counter_seq": counter_seq,
        "seq_monotonic_ok": seq_monotonic_ok,
        "gaps_count": gaps_count,
        "first_gap": first_gap,
        "counter_aligned": counter_aligned,
        "real_events_total": real_total,
        "real_events_with_evidence": real_with_evidence,
        "evidence_coverage_pct": evidence_pct,
        "projected_objects_count": projected_cnt,
        "event_distinct_objects_count": event_objs_cnt,
        "projection_ok": projection_ok,
        "checks": checks,
    }


def format_ledger_audit_text(report: dict[str, Any]) -> str:
    """将账本一致性审计报告格式化为终端中文输出。"""
    verdict = "通过" if report["ok"] else "异常"
    lines = [
        f"账本一致性审计：{report['tenant_id']}（{verdict}，事件 {report['events_total']} 条）",
    ]
    for c in report["checks"]:
        mark = "OK  " if c["ok"] else "FAIL"
        lines.append(f"  [{mark}] {c['name']}: {c['detail']}")
    return "\n".join(lines)
