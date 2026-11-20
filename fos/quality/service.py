"""质量体系核心服务（W22）。

- 待检任务队列：从账本投影直接派生（待检来料批次 + 缺出厂检验的生产批次），不维护冗余任务表；
- 理化检验入账：确定性判定各项指标，不合格自动开启 NCR 并创建待办；
- NCR / 8D 闭环：借助批次追溯自动起草 8D D1–D4 证据，处置让步走审批，关闭由账本守卫校验；
- 质保书（CoA）：仅限真实数据且出厂检验合格的批次；
- 计量器具校准台账与到期提醒；
- ISO 9001 审核证据包：按时间区间从账本确定性汇总质量 KPI 与证据链。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Event, ObjectState, Todo, utcnow
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, AppendResult, Evidence, Ledger
from fos.ledger.report import TraceConfig, report_lines, trace_report
from fos.ontology import Ontology
from fos.orders.data import get
from fos.orders.service import new_id
from fos.quality.config import QualityConfig
from fos.quality.guards import (
    INST_CAL_EVENT,
    INST_REG_EVENT,
    INST_STATUS_EVENT,
    NCR_8D_EVENT,
    NCR_CLOSE_EVENT,
    NCR_DISPOSE_EVENT,
    NCR_OPEN_EVENT,
)
from fos.quality.judge import METRIC_KEYS, evaluate_measurements

SOURCE_TEXT = {
    "incoming": "来料检验",
    "in_process": "过程检验",
    "final": "出厂检验",
    "customer": "客户异议",
}
SEVERITY_TEXT = {"minor": "轻微", "major": "重大", "critical": "严重"}
SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2}
DISP_TEXT = {
    "pending": "待判定",
    "rework": "返工",
    "scrap": "报废",
    "concession": "让步放行",
    "return_supplier": "退回供应商",
}
NCR_STATUS_TEXT = {"open": "待处理", "dispositioned": "已判定处置", "closed": "已关闭"}
D8_TEXT = {"not_required": "无需 8D", "draft": "整改中", "completed": "已完成"}
INST_CAT_TEXT = {
    "thermocouple": "热电偶",
    "balance": "电子天平",
    "press": "压力试验机",
    "caliper": "卡尺/量具",
    "conductivity": "导热系数仪",
    "other": "其他",
}


class QualityError(ValueError):
    """质量业务校验或状态冲突。"""

    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


class QualityNotFound(QualityError):
    def __init__(self, msg: str):
        super().__init__(msg, 404)


class QualityConflict(QualityError):
    def __init__(self, msg: str):
        super().__init__(msg, 409)


async def _scan(session: AsyncSession, tenant: str, obj_type: str, mode: str = "real") -> list[ObjectState]:
    rows = await session.scalars(
        select(ObjectState).where(
            ObjectState.tenant_id == tenant,
            ObjectState.object_type == obj_type,
            ObjectState.data_mode == mode,
        )
    )
    return sorted(rows, key=lambda o: o.created_seq)


# ---------------- 待检任务队列（只读投影） ----------------


async def pending_tasks(session: AsyncSession, tenant: str, *, mode: str = "real") -> dict[str, Any]:
    """待检来料批次 + 缺出厂检验的生产批次，全部从账本投影确定性派生。"""
    lots = await _scan(session, tenant, "material_lot", mode)
    batches = await _scan(session, tenant, "production_batch", mode)
    insps = await _scan(session, tenant, "inspection", mode)
    mats = {m.object_id: m.state for m in await _scan(session, tenant, "material", mode)}
    prods = {p.object_id: p.state for p in await _scan(session, tenant, "product", mode)}

    final_by_batch: dict[str, list[ObjectState]] = {}
    for ins in insps:
        if ins.state.get("kind") == "final" and ins.state.get("batch"):
            final_by_batch.setdefault(ins.state["batch"], []).append(ins)

    incoming: list[dict[str, Any]] = []
    for lot in lots:
        s = lot.state
        if s.get("status") != "pending_inspection":
            continue
        mid = s.get("material")
        m = mats.get(mid or "", {})
        incoming.append(
            {
                "kind": "incoming",
                "object_type": "material_lot",
                "object_id": lot.object_id,
                "material": mid,
                "name": m.get("name") or mid or lot.object_id,
                "qty": s.get("qty"),
                "unit": s.get("unit"),
                "supplier": s.get("supplier"),
                "received_at": s.get("received_at"),
            }
        )

    final_tasks: list[dict[str, Any]] = []
    for b in batches:
        s = b.state
        if s.get("status") not in ("completed", "in_stock"):
            continue
        if final_by_batch.get(b.object_id):
            continue
        pid = s.get("product")
        p = prods.get(pid or "", {})
        final_tasks.append(
            {
                "kind": "final",
                "object_type": "production_batch",
                "object_id": b.object_id,
                "product": pid,
                "name": p.get("name") or p.get("code") or pid or b.object_id,
                "qty": s.get("good_qty") if s.get("good_qty") is not None else s.get("planned_qty"),
                "unit": s.get("unit"),
                "completed_at": s.get("completed_at"),
            }
        )

    return {
        "data_mode": mode,
        "incoming": incoming,
        "final": final_tasks,
        "total": len(incoming) + len(final_tasks),
    }


# ---------------- 检验判定与入账 ----------------


class InspectionIn(BaseModel):
    kind: Literal["incoming", "process", "final"]
    batch: str | None = Field(default=None, max_length=64)
    material_lot: str | None = Field(default=None, max_length=64)
    report_no: str | None = Field(default=None, max_length=64)
    inspected_at: datetime | None = None
    result: Literal["pass", "fail", "conditional"] | None = None
    severity: Literal["minor", "major", "critical"] = "major"
    auto_ncr: bool = True
    note: str | None = Field(default=None, max_length=500)
    bulk_density: FiniteFloat | None = None
    cold_crushing_strength: FiniteFloat | None = None
    thermal_conductivity: FiniteFloat | None = None
    permanent_linear_change: FiniteFloat | None = None
    refractoriness: FiniteFloat | None = None
    al2o3: FiniteFloat | None = None
    fe2o3: FiniteFloat | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    data_mode: Literal["real", "mock"] = "real"


async def record_inspection(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    cfg: QualityConfig,
    tz: ZoneInfo,
    body: InspectionIn,
) -> dict[str, Any]:
    """确定性判定理化检验并入账；不合格且开启自动开单时同步创建 NCR 与质检待办。"""
    tid = principal.tenant_id
    if not body.batch and not body.material_lot:
        raise QualityError("检验记录必须关联生产批次（batch）或原料批次（material_lot）")

    category: str | None = None
    prod_state: dict[str, Any] | None = None
    prod_id: str | None = None
    sup_id: str | None = None
    qty: float | None = None
    unit: str | None = None

    if body.batch:
        b = await get(session, tid, "production_batch", body.batch)
        if b is None:
            raise QualityNotFound(f"生产批次 {body.batch} 不存在")
        prod_id = b.state.get("product")
        qty = b.state.get("good_qty") if b.state.get("good_qty") is not None else b.state.get("planned_qty")
        unit = b.state.get("unit")
        if prod_id:
            p = await get(session, tid, "product", prod_id)
            if p is not None:
                prod_state = p.state
                category = p.state.get("category")
    if body.material_lot:
        lot = await get(session, tid, "material_lot", body.material_lot)
        if lot is None:
            raise QualityNotFound(f"原料批次 {body.material_lot} 不存在")
        sup_id = lot.state.get("supplier")
        qty = qty if qty is not None else lot.state.get("qty")
        unit = unit or lot.state.get("unit")

    measurements = {k: getattr(body, k) for k in METRIC_KEYS if getattr(body, k) is not None}
    judged = evaluate_measurements(cfg, measurements, category=category, product_state=prod_state)

    if body.result is not None:
        if not judged.passed and body.result == "pass":
            raise QualityError(f"指标判定不合格，不能强行记为合格：{judged.summary}")
        final_result = body.result
    elif judged.checks:
        final_result = judged.result
    else:
        raise QualityError("未填检验数值指标时，必须给出人工结论（result）")

    ins_id = new_id("QC", tz)
    at = (body.inspected_at or utcnow()).astimezone(UTC).isoformat()
    payload: dict[str, Any] = {
        "kind": body.kind,
        "inspected_at": at,
        "result": final_result,
        "report_no": body.report_no or ins_id,
        **measurements,
    }
    if body.batch:
        payload["batch"] = body.batch
    if body.material_lot:
        payload["material_lot"] = body.material_lot
    note_parts = [body.note or "", judged.summary]
    payload["note"] = "；".join(x for x in note_parts if x)[:500]
    ev = body.evidence or [Evidence(kind="manual", ref=f"inspection:{ins_id}", note="质检员录入检验判定")]

    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type="quality.batch_inspection_recorded",
            object_id=ins_id,
            payload=payload,
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )

    ncr_info: dict[str, Any] | None = None
    if final_result == "fail" and body.auto_ncr and cfg.ncr.auto_open_on_fail:
        ncr_id = new_id("NCR", tz)
        need_8d = body.severity in cfg.ncr.require_8d_severities
        src = "in_process" if body.kind == "process" else body.kind
        ncr_payload: dict[str, Any] = {
            "ncr_no": ncr_id,
            "source": src,
            "severity": body.severity,
            "inspection": ins_id,
            "summary": f"{SOURCE_TEXT.get(src, src)}不合格：{judged.summary}"[:500],
            "disposition": "pending",
            "d8_status": "draft" if need_8d else "not_required",
            "status": "open",
            "opened_at": at,
        }
        if body.batch:
            ncr_payload["batch"] = body.batch
        if body.material_lot:
            ncr_payload["material_lot"] = body.material_lot
        if prod_id:
            ncr_payload["product"] = prod_id
        if sup_id:
            ncr_payload["supplier"] = sup_id
        if qty is not None:
            ncr_payload["qty"] = qty
        if unit:
            ncr_payload["unit"] = unit
        ncr_ev = [Evidence(kind="calculation", ref=f"inspection:{ins_id}", note=judged.summary[:200])]
        ncr_res = await ledger.append(
            session,
            principal,
            AppendRequest(
                type=NCR_OPEN_EVENT,
                object_id=ncr_id,
                payload=ncr_payload,
                evidence=ncr_ev,
                causation_id=res.event_id,
                data_mode=body.data_mode,
            ),
        )
        session.add(
            Todo(
                tenant_id=tid,
                title=f"不合格品处置：{ncr_id}"[:80],
                body=ncr_payload["summary"],
                assignee_role="qc",
                object_type="ncr",
                object_id=ncr_id,
                source="system:quality",
                data_mode=body.data_mode,
                created_by=principal.subject_id,
            )
        )
        ncr_info = {"id": ncr_id, "event_id": ncr_res.event_id, "seq": ncr_res.seq}

    return {
        "inspection_id": ins_id,
        "event_id": res.event_id,
        "seq": res.seq,
        "result": final_result,
        "judgement": judged.out(),
        "ncr": ncr_info,
        "data_mode": body.data_mode,
    }


# ---------------- NCR 与 8D 闭环 ----------------


class NcrOpenIn(BaseModel):
    source: Literal["incoming", "in_process", "final", "customer"]
    severity: Literal["minor", "major", "critical"] = "major"
    inspection: str | None = Field(default=None, max_length=64)
    material_lot: str | None = Field(default=None, max_length=64)
    batch: str | None = Field(default=None, max_length=64)
    product: str | None = Field(default=None, max_length=64)
    supplier: str | None = Field(default=None, max_length=64)
    summary: str = Field(min_length=1, max_length=500)
    qty: FiniteFloat | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, max_length=24)
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    data_mode: Literal["real", "mock"] = "real"


class NcrDispositionIn(BaseModel):
    disposition: Literal["rework", "scrap", "concession", "return_supplier"]
    disposition_note: str = Field(default="", max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


class Ncr8dIn(BaseModel):
    containment: str | None = Field(default=None, max_length=1000)
    root_cause: str | None = Field(default=None, max_length=1000)
    corrective_action: str | None = Field(default=None, max_length=1000)
    preventive_action: str | None = Field(default=None, max_length=1000)
    verification: str | None = Field(default=None, max_length=1000)
    d8_status: Literal["not_required", "draft", "completed"] | None = None


class NcrCloseIn(BaseModel):
    verification: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def ncr_out(o: ObjectState) -> dict[str, Any]:
    s = o.state
    return {
        "id": o.object_id,
        "ncr_no": s.get("ncr_no") or o.object_id,
        "source": s.get("source"),
        "source_text": SOURCE_TEXT.get(s.get("source") or "", s.get("source") or ""),
        "severity": s.get("severity"),
        "severity_text": SEVERITY_TEXT.get(s.get("severity") or "", s.get("severity") or ""),
        "inspection": s.get("inspection"),
        "material_lot": s.get("material_lot"),
        "batch": s.get("batch"),
        "product": s.get("product"),
        "supplier": s.get("supplier"),
        "summary": s.get("summary"),
        "qty": s.get("qty"),
        "unit": s.get("unit"),
        "disposition": s.get("disposition") or "pending",
        "disposition_text": DISP_TEXT.get(s.get("disposition") or "pending", ""),
        "disposition_note": s.get("disposition_note"),
        "containment": s.get("containment"),
        "root_cause": s.get("root_cause"),
        "corrective_action": s.get("corrective_action"),
        "preventive_action": s.get("preventive_action"),
        "verification": s.get("verification"),
        "d8_status": s.get("d8_status") or "not_required",
        "d8_text": D8_TEXT.get(s.get("d8_status") or "not_required", ""),
        "status": s.get("status"),
        "status_text": NCR_STATUS_TEXT.get(s.get("status") or "", ""),
        "opened_at": s.get("opened_at"),
        "closed_at": s.get("closed_at"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def ncr_board(
    session: AsyncSession, tenant: str, *, status: str | None = None, mode: str = "real"
) -> list[dict[str, Any]]:
    rows = [ncr_out(o) for o in await _scan(session, tenant, "ncr", mode)]
    if status:
        rows = [r for r in rows if r["status"] == status]
    rows.sort(
        key=lambda r: (
            r["status"] == "closed",
            SEVERITY_RANK.get(r["severity"] or "", 9),
            r["id"],
        )
    )
    return rows


async def open_ncr(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    cfg: QualityConfig,
    tz: ZoneInfo,
    body: NcrOpenIn,
) -> dict[str, Any]:
    ncr_id = new_id("NCR", tz)
    need_8d = body.severity in cfg.ncr.require_8d_severities
    payload: dict[str, Any] = {
        "ncr_no": ncr_id,
        "source": body.source,
        "severity": body.severity,
        "summary": body.summary,
        "disposition": "pending",
        "d8_status": "draft" if need_8d else "not_required",
        "status": "open",
        "opened_at": utcnow().isoformat(),
    }
    for k in ("inspection", "material_lot", "batch", "product", "supplier", "qty", "unit"):
        if getattr(body, k) is not None:
            payload[k] = getattr(body, k)
    ev = body.evidence or [Evidence(kind="manual", ref=f"ncr:{ncr_id}", note="人工开启 NCR")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=NCR_OPEN_EVENT,
            object_id=ncr_id,
            payload=payload,
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )
    return {"id": ncr_id, "event_id": res.event_id, "seq": res.seq}


async def disposition_ncr(
    session: AsyncSession, principal: Principal, ledger: Ledger, ncr_id: str, body: NcrDispositionIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "ncr", ncr_id)
    if o is None:
        raise QualityNotFound(f"不合格品报告 {ncr_id} 不存在")
    if o.state.get("status") == "closed":
        raise QualityConflict("不合格品报告已关闭")
    ev = body.evidence or [Evidence(kind="manual", ref=f"ncr:{ncr_id}:disp", note=body.disposition_note)]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=NCR_DISPOSE_EVENT,
            object_id=ncr_id,
            payload={
                "disposition": body.disposition,
                "disposition_note": body.disposition_note,
                "status": "dispositioned",
            },
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def update_8d(
    session: AsyncSession, principal: Principal, ledger: Ledger, ncr_id: str, body: Ncr8dIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "ncr", ncr_id)
    if o is None:
        raise QualityNotFound(f"不合格品报告 {ncr_id} 不存在")
    if o.state.get("status") == "closed":
        raise QualityConflict("不合格品报告已关闭")
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if not payload:
        raise QualityError("至少填写一项 8D 字段")
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=NCR_8D_EVENT,
            object_id=ncr_id,
            payload=payload,
            evidence=[Evidence(kind="manual", ref=f"ncr:{ncr_id}:8d", note="更新 8D 整改记录")],
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def close_ncr(
    session: AsyncSession, principal: Principal, ledger: Ledger, ncr_id: str, body: NcrCloseIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "ncr", ncr_id)
    if o is None:
        raise QualityNotFound(f"不合格品报告 {ncr_id} 不存在")
    ev = body.evidence or [Evidence(kind="manual", ref=f"ncr:{ncr_id}:close", note=body.verification[:200])]
    payload = {"status": "closed", "closed_at": utcnow().isoformat(), "verification": body.verification}
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=NCR_CLOSE_EVENT,
            object_id=ncr_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def draft_8d(
    session: AsyncSession,
    onto: Ontology,
    trace_cfg: TraceConfig | None,
    principal: Principal,
    ncr_id: str,
) -> dict[str, Any]:
    """L1 辅助：基于账本批次追溯确定性汇总 8D D1–D4 线索草稿，供质检/工艺确认。"""
    o = await get(session, principal.tenant_id, "ncr", ncr_id)
    if o is None:
        raise QualityNotFound(f"不合格品报告 {ncr_id} 不存在")
    s = o.state
    clues: list[str] = []
    alerts: list[dict[str, Any]] = []
    root_type, root_id = (
        ("production_batch", s["batch"])
        if s.get("batch")
        else ("material_lot", s["material_lot"])
        if s.get("material_lot")
        else ("inspection", s["inspection"])
        if s.get("inspection")
        else (None, None)
    )
    if root_type and root_id and trace_cfg is not None:
        rep = await trace_report(session, onto, trace_cfg, principal, root_type, root_id)
        alerts = rep.get("alerts") or []
        for a in alerts:
            clues.append(f"追溯告警：{a['object_id']} {a['text']}")
        for group, text in report_lines(onto, trace_cfg, rep):
            clues.append(f"[{group}] {text}")

    containment_hint = (
        f"隔离批次 {s.get('batch') or s.get('material_lot') or '相关在制品'}，暂停发货/领用并排查同窑次产品。"
    )
    root_hint = "；".join(clues[:4]) if clues else "请结合来料、配方、成型与烧成曲线排查根因。"
    return {
        "ncr_id": ncr_id,
        "ncr_no": s.get("ncr_no") or ncr_id,
        "data_mode": o.data_mode,
        "d1_team": ["质检员", "班组长", "工艺工程师"],
        "d2_problem": s.get("summary") or "",
        "d3_containment": s.get("containment") or containment_hint,
        "d4_root_cause_clues": clues[:12],
        "d4_suggested": s.get("root_cause") or root_hint,
        "d5_corrective": s.get("corrective_action") or "",
        "d7_preventive": s.get("preventive_action") or "",
        "d8_verification": s.get("verification") or "",
        "trace_alerts": alerts,
    }


# ---------------- 质保书（CoA） ----------------


async def certificate_data(
    session: AsyncSession, tenant: str, cfg: QualityConfig, batch_id: str
) -> dict[str, Any]:
    """按生产批次确定性拼装质保书数据（禁止模拟数据、必须有合格出厂检验）。"""
    b = await get(session, tenant, "production_batch", batch_id)
    if b is None:
        raise QualityNotFound(f"生产批次 {batch_id} 不存在")
    if b.data_mode == "mock":
        raise QualityConflict("模拟数据不能签发正式质保书")
    bs = b.state
    insps = [
        ins
        for ins in await _scan(session, tenant, "inspection", "real")
        if ins.state.get("batch") == batch_id and ins.state.get("kind") == "final"
    ]
    if not insps:
        raise QualityConflict(f"批次 {batch_id} 还没有出厂检验记录，不能生成质保书")
    latest = insps[-1]
    if latest.state.get("result") != "pass":
        raise QualityConflict(f"批次 {batch_id} 最新出厂检验结论不是合格，不能签发质保书")

    prod = await get(session, tenant, "product", bs.get("product") or "") if bs.get("product") else None
    ps = prod.state if prod else {}
    kr = await get(session, tenant, "kiln_run", bs.get("kiln_run") or "") if bs.get("kiln_run") else None
    order = await get(session, tenant, "sales_order", bs.get("order") or "") if bs.get("order") else None
    cust = (
        await get(session, tenant, "customer", order.state.get("customer") or "")
        if order and order.state.get("customer")
        else None
    )

    measurements = {k: latest.state.get(k) for k in METRIC_KEYS if latest.state.get(k) is not None}
    judged = evaluate_measurements(cfg, measurements, category=ps.get("category"), product_state=ps)

    return {
        "certificate_no": f"COA-{batch_id}",
        "title": cfg.certificate.issuer_title,
        "standard_ref": cfg.certificate.standard_ref,
        "footer_note": cfg.certificate.footer_note,
        "issued_at": utcnow().isoformat(),
        "batch_id": batch_id,
        "qty": bs.get("good_qty") if bs.get("good_qty") is not None else bs.get("planned_qty"),
        "unit": bs.get("unit") or "",
        "product_code": ps.get("code") or bs.get("product") or "",
        "product_name": ps.get("name") or "",
        "customer": cust.state.get("name") if cust else "",
        "order_no": order.state.get("order_no") if order else "",
        "kiln_run": kr.state.get("run_no") if kr else (bs.get("kiln_run") or ""),
        "inspection_id": latest.object_id,
        "report_no": latest.state.get("report_no") or latest.object_id,
        "inspected_at": latest.state.get("inspected_at") or "",
        "result": "合格",
        "checks": [c.out() for c in judged.checks],
        "data_mode": "real",
    }


# ---------------- 计量器具校准台账与到期提醒 ----------------


class InstrumentIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    category: Literal["thermocouple", "balance", "press", "caliper", "conductivity", "other"] = "other"
    serial_no: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=100)
    calibration_cycle_days: int = Field(default=365, ge=7, le=3650)
    last_calibrated_on: date | None = None
    next_due_date: date
    certificate_no: str | None = Field(default=None, max_length=64)
    agency: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=500)
    data_mode: Literal["real", "mock"] = "real"


class CalibrateIn(BaseModel):
    last_calibrated_on: date
    next_due_date: date | None = None
    certificate_no: str = Field(min_length=1, max_length=64)
    agency: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def instrument_out(o: ObjectState, cfg: QualityConfig, today: date) -> dict[str, Any]:
    s = o.state
    due_raw = s.get("next_due_date")
    due = date.fromisoformat(due_raw) if due_raw else None
    days_left = (due - today).days if due else None
    if s.get("status") == "out_of_service":
        cal_state, cal_text = "out_of_service", "已停用"
    elif days_left is not None and days_left < 0:
        cal_state, cal_text = "overdue", f"已超期 {-days_left} 天"
    elif days_left is not None and days_left <= cfg.calibration.remind_days:
        cal_state, cal_text = "due_soon", f"{days_left} 天后到期"
    else:
        cal_state, cal_text = "ok", "正常"
    return {
        "id": o.object_id,
        "code": s.get("code") or o.object_id,
        "name": s.get("name") or o.object_id,
        "category": s.get("category") or "other",
        "category_text": INST_CAT_TEXT.get(s.get("category") or "other", "其他"),
        "serial_no": s.get("serial_no"),
        "location": s.get("location"),
        "calibration_cycle_days": s.get("calibration_cycle_days"),
        "last_calibrated_on": s.get("last_calibrated_on"),
        "next_due_date": due_raw,
        "days_left": days_left,
        "certificate_no": s.get("certificate_no"),
        "agency": s.get("agency"),
        "status": s.get("status"),
        "cal_state": cal_state,
        "cal_text": cal_text,
        "note": s.get("note"),
        "data_mode": o.data_mode,
    }


async def instruments_board(
    session: AsyncSession, tenant: str, cfg: QualityConfig, today: date, *, mode: str = "real"
) -> dict[str, Any]:
    rows = await _scan(session, tenant, "measuring_instrument", mode)
    items = [instrument_out(o, cfg, today) for o in rows]
    order = {"overdue": 0, "due_soon": 1, "ok": 2, "out_of_service": 3}
    items.sort(key=lambda x: (order.get(x["cal_state"], 9), x["next_due_date"] or "9999-12-31", x["code"]))
    return {
        "date": today.isoformat(),
        "data_mode": mode,
        "items": items,
        "counts": {
            "total": len(items),
            "overdue": sum(1 for x in items if x["cal_state"] == "overdue"),
            "due_soon": sum(1 for x in items if x["cal_state"] == "due_soon"),
            "ok": sum(1 for x in items if x["cal_state"] == "ok"),
        },
    }


async def register_instrument(
    session: AsyncSession, principal: Principal, ledger: Ledger, body: InstrumentIn
) -> AppendResult:
    oid = f"MI-{body.code}"[:64]
    payload: dict[str, Any] = {
        "code": body.code,
        "name": body.name,
        "category": body.category,
        "calibration_cycle_days": body.calibration_cycle_days,
        "next_due_date": body.next_due_date.isoformat(),
        "status": "ok",
    }
    if body.last_calibrated_on:
        payload["last_calibrated_on"] = body.last_calibrated_on.isoformat()
    for k in ("serial_no", "location", "certificate_no", "agency", "note"):
        if getattr(body, k):
            payload[k] = getattr(body, k)
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=INST_REG_EVENT,
            object_id=oid,
            payload=payload,
            evidence=[Evidence(kind="manual", ref=f"instrument:{oid}", note="登记计量器具")],
            data_mode=body.data_mode,
        ),
    )


async def record_calibration(
    session: AsyncSession, principal: Principal, ledger: Ledger, inst_id: str, body: CalibrateIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "measuring_instrument", inst_id)
    if o is None:
        raise QualityNotFound(f"计量器具 {inst_id} 不存在")
    cycle = int(o.state.get("calibration_cycle_days") or 365)
    next_due = body.next_due_date or (body.last_calibrated_on + timedelta(days=cycle))
    payload: dict[str, Any] = {
        "last_calibrated_on": body.last_calibrated_on.isoformat(),
        "next_due_date": next_due.isoformat(),
        "certificate_no": body.certificate_no,
        "status": "ok",
    }
    if body.agency:
        payload["agency"] = body.agency
    if body.note:
        payload["note"] = body.note
    ev = body.evidence or [Evidence(kind="document", ref=f"cal:{body.certificate_no}", note="校准证书登记")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=INST_CAL_EVENT,
            object_id=inst_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def remind_calibration_due(
    session: AsyncSession, tenant: str, cfg: QualityConfig, today: date, *, mode: str = "real"
) -> dict[str, Any]:
    """为即将到期或已超期的计量器具生成去重待办（同一器具、同一到期日只建一条未关闭待办）。"""
    board = await instruments_board(session, tenant, cfg, today, mode=mode)
    existing = {
        (t.object_id, t.due_at.date().isoformat() if t.due_at else "")
        for t in (
            await session.scalars(
                select(Todo).where(
                    Todo.tenant_id == tenant,
                    Todo.object_type == "measuring_instrument",
                    Todo.status == "open",
                    Todo.data_mode == mode,
                )
            )
        ).all()
    }
    created: list[str] = []
    for it in board["items"]:
        if it["cal_state"] not in ("overdue", "due_soon"):
            continue
        due_str = it["next_due_date"] or ""
        if (it["id"], due_str) in existing:
            continue
        due_dt = datetime.combine(date.fromisoformat(due_str), time(17, 0), tzinfo=UTC) if due_str else None
        prefix = "【超期】" if it["cal_state"] == "overdue" else "【即将到期】"
        session.add(
            Todo(
                tenant_id=tenant,
                title=f"{prefix}计量器具校准：{it['name']}（{it['code']}）"[:80],
                body=f"到期日 {due_str}（{it['cal_text']}），位置：{it['location'] or '未填'}。",
                assignee_role=cfg.calibration.notify_roles[0] if cfg.calibration.notify_roles else "qc",
                object_type="measuring_instrument",
                object_id=it["id"],
                due_at=due_dt,
                source="system:calibration",
                data_mode=mode,
                created_by="system",
            )
        )
        created.append(it["id"])
    await session.flush()
    return {"created": created, "count": len(created), "counts": board["counts"]}


# ---------------- ISO 9001 审核证据包 ----------------


async def iso_audit_pack(
    session: AsyncSession,
    tenant: str,
    cfg: QualityConfig,
    start_day: date,
    end_day: date,
    tz: ZoneInfo,
    *,
    mode: str = "real",
) -> dict[str, Any]:
    """按日期区间从账本确定性汇总 ISO 9001 审核所需的质量 KPI 与证据清单。"""
    if end_day < start_day:
        raise QualityError("结束日期不能早于开始日期")
    t_start = datetime.combine(start_day, time(), tzinfo=tz).astimezone(UTC)
    t_end = datetime.combine(end_day + timedelta(days=1), time(), tzinfo=tz).astimezone(UTC)

    evs = list(
        await session.scalars(
            select(Event)
            .where(
                Event.tenant_id == tenant,
                Event.data_mode == mode,
                Event.occurred_at >= t_start,
                Event.occurred_at < t_end,
            )
            .order_by(Event.seq)
        )
    )
    ins_evs = [
        e for e in evs if e.type in ("quality.inspection_recorded", "quality.batch_inspection_recorded")
    ]
    ncr_open_evs = [e for e in evs if e.type == NCR_OPEN_EVENT]
    cal_evs = [e for e in evs if e.type == INST_CAL_EVENT]

    def _pass_rate(items: list[Event]) -> float | None:
        if not items:
            return None
        ok = sum(1 for e in items if e.payload.get("result") == "pass")
        return round(100.0 * ok / len(items), 1)

    iqc = [e for e in ins_evs if e.payload.get("kind") == "incoming"]
    fqc = [e for e in ins_evs if e.payload.get("kind") == "final"]

    ncr_ids = {e.object_id for e in ncr_open_evs}
    ncr_objs = [o for o in await _scan(session, tenant, "ncr", mode) if o.object_id in ncr_ids]
    ncr_closed = sum(1 for o in ncr_objs if o.state.get("status") == "closed")
    d8_req = [o for o in ncr_objs if o.state.get("severity") in cfg.ncr.require_8d_severities]
    d8_done = sum(1 for o in d8_req if o.state.get("d8_status") == "completed")

    inst_board = await instruments_board(session, tenant, cfg, end_day, mode=mode)
    active_inst = [x for x in inst_board["items"] if x["cal_state"] != "out_of_service"]
    cal_ok = sum(1 for x in active_inst if x["cal_state"] != "overdue")
    cal_rate = round(100.0 * cal_ok / len(active_inst), 1) if active_inst else None

    return {
        "period": {"start": start_day.isoformat(), "end": end_day.isoformat()},
        "data_mode": mode,
        "kpis": {
            "inspections_total": len(ins_evs),
            "iqc_total": len(iqc),
            "iqc_pass_rate": _pass_rate(iqc),
            "fqc_total": len(fqc),
            "fqc_pass_rate": _pass_rate(fqc),
            "ncr_opened": len(ncr_objs),
            "ncr_closed": ncr_closed,
            "ncr_close_rate": round(100.0 * ncr_closed / len(ncr_objs), 1) if ncr_objs else None,
            "d8_required": len(d8_req),
            "d8_completed": d8_done,
            "instruments_active": len(active_inst),
            "calibration_on_time_rate": cal_rate,
            "calibrations_in_period": len(cal_evs),
        },
        "ncrs": [ncr_out(o) for o in ncr_objs],
        "instruments": inst_board["items"],
        "evidence_events": [
            {"seq": e.seq, "type": e.type, "object_id": e.object_id, "at": e.occurred_at.isoformat()}
            for e in (ins_evs + ncr_open_evs + cal_evs)[:200]
        ],
    }


__all__ = [
    "INST_STATUS_EVENT",
    "CalibrateIn",
    "InspectionIn",
    "InstrumentIn",
    "Ncr8dIn",
    "NcrCloseIn",
    "NcrDispositionIn",
    "NcrOpenIn",
    "QualityConflict",
    "QualityError",
    "QualityNotFound",
    "certificate_data",
    "close_ncr",
    "disposition_ncr",
    "draft_8d",
    "instrument_out",
    "instruments_board",
    "iso_audit_pack",
    "ncr_board",
    "ncr_out",
    "open_ncr",
    "pending_tasks",
    "record_calibration",
    "record_inspection",
    "register_instrument",
    "remind_calibration_due",
    "update_8d",
]
