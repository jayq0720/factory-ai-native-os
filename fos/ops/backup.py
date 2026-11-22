"""单租户不可变事件账本防篡改 JSONL 灾备导出、离线完整性校验与审计流水 Excel 导出。"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Event, TenantCounter, utcnow
from fos.excel_safe import excel_row
from fos.ledger.projection import rebuild
from fos.ontology import Ontology


def _canonical_event(e: Event) -> dict[str, Any]:
    return {
        "seq": int(e.seq),
        "id": e.id,
        "tenant_id": e.tenant_id,
        "type": e.type,
        "object_type": e.object_type,
        "object_id": e.object_id,
        "payload": e.payload,
        "evidence": e.evidence or [],
        "actor_kind": e.actor_kind,
        "actor_id": e.actor_id,
        "actor_name": e.actor_name,
        "data_mode": e.data_mode,
        "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
        "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
        "idempotency_key": e.idempotency_key,
        "causation_id": e.causation_id,
        "correlation_id": e.correlation_id,
        "ontology_layers": e.ontology_layers or [],
    }


def _dump_line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def export_tenant_ledger_jsonl(
    session: AsyncSession,
    tenant_id: str,
    *,
    data_mode: str | None = "real",
) -> tuple[str, dict[str, Any]]:
    """按 seq 升序导出租户不可变事件账本为带 SHA-256 防篡改校验和的 JSONL。"""
    conds: list[Any] = [Event.tenant_id == tenant_id]
    if data_mode is not None:
        conds.append(Event.data_mode == data_mode)
    rows = (await session.scalars(select(Event).where(*conds).order_by(Event.seq.asc()))).all()

    first_seq = int(rows[0].seq) if rows else None
    last_seq = int(rows[-1].seq) if rows else None
    meta = {
        "format": "fos-ledger-backup-v1",
        "tenant_id": tenant_id,
        "data_mode": data_mode or "all",
        "count": len(rows),
        "first_seq": first_seq,
        "last_seq": last_seq,
        "exported_at": utcnow().isoformat(),
    }

    hasher = hashlib.sha256()
    lines = [_dump_line({"_meta": meta})]
    for e in rows:
        line = _dump_line(_canonical_event(e))
        hasher.update(line.encode("utf-8"))
        hasher.update(b"\n")
        lines.append(line)

    sha256 = hasher.hexdigest()
    lines.append(_dump_line({"_checksum": {"sha256": sha256, "count": len(rows)}}))
    summary = {**meta, "sha256": sha256}
    return "\n".join(lines) + "\n", summary


def verify_ledger_backup_jsonl(content: str) -> dict[str, Any]:
    """离线校验账本灾备 JSONL 的元数据头、租户隔离、seq 单调性与 SHA-256 完整性。"""
    raw_lines = [ln for ln in content.splitlines() if ln.strip()]
    if len(raw_lines) < 2:
        return {"ok": False, "error": "备份文件缺少元数据头或校验尾"}

    try:
        head = json.loads(raw_lines[0])
        tail = json.loads(raw_lines[-1])
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"头尾 JSON 解析失败：{e}"}

    meta = head.get("_meta") if isinstance(head, dict) else None
    chk = tail.get("_checksum") if isinstance(tail, dict) else None
    if not isinstance(meta, dict) or meta.get("format") != "fos-ledger-backup-v1":
        return {"ok": False, "error": "不支持的备份格式或缺少 _meta"}
    if not isinstance(chk, dict) or not isinstance(chk.get("sha256"), str):
        return {"ok": False, "error": "缺少 _checksum SHA-256 校验尾"}

    tenant_id = str(meta.get("tenant_id") or "")
    hasher = hashlib.sha256()
    prev_seq: int | None = None
    event_lines = raw_lines[1:-1]
    for idx, line in enumerate(event_lines, start=1):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"第 {idx} 条事件 JSON 损坏：{e}"}
        if not isinstance(ev, dict) or ev.get("tenant_id") != tenant_id:
            return {"ok": False, "error": f"第 {idx} 条事件租户不匹配（期望 {tenant_id}）"}
        seq = ev.get("seq")
        if not isinstance(seq, int) or (prev_seq is not None and seq <= prev_seq):
            return {"ok": False, "error": f"第 {idx} 条事件 seq 非严格递增（{seq} <= {prev_seq}）"}
        prev_seq = seq
        hasher.update(line.encode("utf-8"))
        hasher.update(b"\n")

    actual_count = len(event_lines)
    m_cnt, c_cnt = meta.get("count"), chk.get("count")
    if actual_count != int(m_cnt if m_cnt is not None else -1) or actual_count != int(
        c_cnt if c_cnt is not None else -1
    ):
        return {"ok": False, "error": f"事件条数不一致：实际 {actual_count}，头 {m_cnt}，尾 {c_cnt}"}
    actual_sha = hasher.hexdigest()
    if actual_sha != chk["sha256"]:
        return {"ok": False, "error": "SHA-256 校验和不匹配，文件可能被篡改或截断"}

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "data_mode": meta.get("data_mode"),
        "count": actual_count,
        "first_seq": meta.get("first_seq"),
        "last_seq": meta.get("last_seq"),
        "sha256": actual_sha,
    }


def render_audit_xlsx(audit_data: dict[str, Any]) -> bytes:
    """将审计流水查询结果导出为防公式注入的合规审计 Excel。"""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "不可变账本审计流水"
    headers = [
        "序号",
        "事件 ID",
        "事件类型",
        "对象类型",
        "对象 ID",
        "操作者",
        "角色类型",
        "数据模式",
        "证据数",
        "发生时间",
    ]
    ws.append(excel_row(headers))
    for it in audit_data.get("items") or []:
        ws.append(
            excel_row(
                [
                    it.get("seq"),
                    it.get("id"),
                    it.get("type"),
                    it.get("object_type"),
                    it.get("object_id"),
                    it.get("actor_name"),
                    it.get("actor_kind"),
                    it.get("data_mode"),
                    it.get("evidence_count"),
                    it.get("occurred_at"),
                ]
            )
        )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _parse_dt(val: str | None) -> datetime:
    return datetime.fromisoformat(val) if val else utcnow()


async def restore_tenant_ledger_jsonl(
    session: AsyncSession,
    onto: Ontology,
    tenant_id: str,
    content: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """校验防篡改灾备 JSONL 后原子恢复单租户事件账本，同步 TenantCounter 并重建全部投影。"""
    ver = verify_ledger_backup_jsonl(content)
    if not ver["ok"]:
        raise ValueError(f"灾备文件校验失败：{ver['error']}")
    if ver["tenant_id"] != tenant_id:
        raise ValueError(f"租户不匹配：备份属于 {ver['tenant_id']}，目标租户为 {tenant_id}")

    existing = int(
        (await session.scalar(select(func.count()).select_from(Event).where(Event.tenant_id == tenant_id)))
        or 0
    )
    if existing > 0 and not force:
        raise ValueError(f"租户 {tenant_id} 已有 {existing} 条事件，请加 --force 确认覆盖恢复")

    if existing > 0:
        await session.execute(delete(Event).where(Event.tenant_id == tenant_id))
        await session.flush()

    raw_lines = [ln for ln in content.splitlines() if ln.strip()]
    max_seq = 0
    restored = 0
    for line in raw_lines[1:-1]:
        ev = json.loads(line)
        seq = int(ev["seq"])
        max_seq = max(max_seq, seq)
        session.add(
            Event(
                id=ev["id"],
                tenant_id=tenant_id,
                seq=seq,
                type=ev["type"],
                object_type=ev["object_type"],
                object_id=ev["object_id"],
                payload=ev["payload"],
                evidence=ev.get("evidence") or [],
                actor_kind=ev["actor_kind"],
                actor_id=ev["actor_id"],
                actor_name=ev["actor_name"],
                data_mode=ev.get("data_mode") or "real",
                occurred_at=_parse_dt(ev.get("occurred_at")),
                recorded_at=_parse_dt(ev.get("recorded_at")),
                idempotency_key=ev.get("idempotency_key"),
                causation_id=ev.get("causation_id"),
                correlation_id=ev.get("correlation_id"),
                ontology_layers=ev.get("ontology_layers") or [],
            )
        )
        restored += 1

    await session.flush()
    ctr = await session.get(TenantCounter, tenant_id)
    if ctr is None:
        session.add(TenantCounter(tenant_id=tenant_id, last_seq=max_seq))
    else:
        ctr.last_seq = max_seq
    await session.flush()

    replayed = await rebuild(session, onto, tenant_id)
    return {
        "tenant_id": tenant_id,
        "restored_events": restored,
        "replayed_events": replayed,
        "last_seq": max_seq,
        "sha256": ver["sha256"],
    }
