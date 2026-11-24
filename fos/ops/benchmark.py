"""不可变事件账本写入、实时一致性审计与防篡改灾备性能基准引擎。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Event, ObjectState, TenantCounter
from fos.ops.backup import export_tenant_ledger, verify_backup_file
from fos.ops.ledger_audit import verify_tenant_ledger


async def run_ledger_benchmark(
    session: AsyncSession,
    tenant_id: str,
    *,
    events_count: int = 1000,
    objects_count: int = 50,
    allow_nonempty: bool = False,
) -> dict[str, Any]:
    """在指定租户执行账本写入 + 一致性审计 + 灾备导出/校验性能基准。默认仅允许空租户。"""
    n_ev = max(1, int(events_count))
    n_obj = max(1, min(n_ev, int(objects_count)))

    q_cnt = select(func.count(Event.id)).where(Event.tenant_id == tenant_id)
    existing = int((await session.execute(q_cnt)).scalar_one() or 0)
    if existing > 0 and not allow_nonempty:
        raise ValueError(f"租户 {tenant_id!r} 已有 {existing} 条事件，请换空租户或加 --allow-nonempty")

    ctr = await session.get(TenantCounter, tenant_id)
    base_seq = int(ctr.last_seq) if ctr is not None else 0

    t0 = perf_counter()
    objs_seen: dict[str, int] = {}
    batch_events: list[Event] = []
    for i in range(1, n_ev + 1):
        seq = base_seq + i
        oid = f"bench_obj_{(i - 1) % n_obj:05d}"
        objs_seen[oid] = seq
        batch_events.append(
            Event(
                tenant_id=tenant_id,
                seq=seq,
                event_type="bench.recorded",
                object_type="batch",
                object_id=oid,
                payload={"idx": i, "qty": 100.0},
                evidence=[{"kind": "benchmark", "ref": f"b-{i}"}],
                actor="benchmark",
                data_mode="mock",
            )
        )
    session.add_all(batch_events)
    if ctr is None:
        session.add(TenantCounter(tenant_id=tenant_id, last_seq=base_seq + n_ev))
    else:
        ctr.last_seq = base_seq + n_ev

    for oid, last_seq in objs_seen.items():
        st = await session.get(ObjectState, (tenant_id, "batch", oid))
        if st is None:
            session.add(
                ObjectState(
                    tenant_id=tenant_id,
                    object_type="batch",
                    object_id=oid,
                    state={"last_seq": last_seq},
                    last_seq=last_seq,
                )
            )
        else:
            st.last_seq = last_seq
            st.state = {"last_seq": last_seq}
    await session.flush()
    write_ms = (perf_counter() - t0) * 1000.0

    t1 = perf_counter()
    audit = await verify_tenant_ledger(session, tenant_id)
    audit_ms = (perf_counter() - t1) * 1000.0

    with tempfile.TemporaryDirectory(prefix="fos-bench-") as tmp:
        out_path = Path(tmp) / f"{tenant_id}.jsonl"
        t2 = perf_counter()
        exp = await export_tenant_ledger(session, tenant_id, out_path, include_mock=True)
        export_ms = (perf_counter() - t2) * 1000.0

        t3 = perf_counter()
        ver = verify_backup_file(out_path)
        verify_ms = (perf_counter() - t3) * 1000.0

    write_eps = round(n_ev / max(write_ms / 1000.0, 1e-6), 1)
    return {
        "tenant_id": tenant_id,
        "events_written": n_ev,
        "objects_count": n_obj,
        "write_ms": round(write_ms, 2),
        "write_events_per_sec": write_eps,
        "audit_ms": round(audit_ms, 2),
        "audit_ok": bool(audit["ok"]),
        "export_ms": round(export_ms, 2),
        "export_events": int(exp["events_count"]),
        "verify_backup_ms": round(verify_ms, 2),
        "verify_backup_ok": bool(ver["ok"]),
    }


def format_benchmark_text(rep: dict[str, Any]) -> str:
    head = f"账本基准 [{rep['tenant_id']}]：{rep['events_written']} 条事件 / {rep['objects_count']} 对象"
    return "\n".join(
        [
            head,
            f"  写入吞吐：{rep['write_ms']} ms（{rep['write_events_per_sec']} events/s）",
            f"  实时审计：{rep['audit_ms']} ms（ok={rep['audit_ok']}）",
            f"  灾备导出：{rep['export_ms']} ms（{rep['export_events']} 条）",
            f"  灾备校验：{rep['verify_backup_ms']} ms（ok={rep['verify_backup_ok']}）",
        ]
    )
