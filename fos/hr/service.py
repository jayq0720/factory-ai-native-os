"""人力、排班、计件与特种资质核心服务（W26）。

- 员工花名册：建档、调岗调薪/离职更新；
- 特种作业资质：登记、复审换证、到期/临期看板与去重待办提醒；
- 班次排班与计件考勤：记录出勤、工时、加班、计件产量；
- 请假单：3 天及以上走总经理审批策略，支持撤销；
- 月度薪酬核算：确定性汇总底薪折算、加班费与计件工资。
"""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState, Todo
from fos.governance.principal import Principal
from fos.hr.config import HrConfig
from fos.hr.guards import (
    CERT_REC_EVENT,
    CERT_RENEW_EVENT,
    EMP_REG_EVENT,
    EMP_UPD_EVENT,
    LEAVE_CANCEL_EVENT,
    LEAVE_SUB_EVENT,
    ROSTER_LOG_EVENT,
)
from fos.hr.payroll import compute_payroll
from fos.ledger import AppendRequest, AppendResult, Evidence, Ledger
from fos.orders.data import get
from fos.orders.service import new_id

EMP_STATUS_TEXT = {"active": "在职", "on_leave": "休假", "resigned": "已离职"}
CERT_KIND_TEXT = {
    "welder": "电焊/动火证",
    "electrician": "电工证",
    "forklift": "叉车证",
    "crane": "行车/起重证",
    "kiln_operator": "司炉/窑炉操作证",
    "safety_officer": "安全员证",
    "other": "其他资质",
}
CERT_STATE_TEXT = {"ok": "有效", "expiring": "临期", "expired": "已过期", "revoked": "已吊销"}
SHIFT_TEXT = {"day": "白班", "night": "夜班", "morning": "早班", "afternoon": "中班"}
ATTEND_TEXT = {
    "scheduled": "已排班",
    "present": "出勤",
    "late": "迟到",
    "absent": "缺勤",
    "leave": "请假",
}
LEAVE_KIND_TEXT = {"annual": "年假", "sick": "病假", "personal": "事假", "injury": "工伤假"}
LEAVE_STATUS_TEXT = {"approved": "已批准", "cancelled": "已撤销"}


class HrError(ValueError):
    """人力模块业务校验或状态冲突。"""

    def __init__(self, msg: str, status: int = 422):
        super().__init__(msg)
        self.status = status


class HrNotFound(HrError):
    def __init__(self, msg: str):
        super().__init__(msg, 404)


async def _scan(session: AsyncSession, tenant: str, obj_type: str, mode: str = "real") -> list[ObjectState]:
    rows = await session.scalars(
        select(ObjectState).where(
            ObjectState.tenant_id == tenant,
            ObjectState.object_type == obj_type,
            ObjectState.data_mode == mode,
        )
    )
    return sorted(rows, key=lambda o: o.created_seq)


# ---------------- 员工花名册 ----------------


class EmployeeIn(BaseModel):
    emp_no: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    department: str = Field(min_length=1, max_length=64)
    position: str = Field(min_length=1, max_length=64)
    shift_group: Literal["A", "B", "C", "day"] = "day"
    base_salary: FiniteFloat = Field(default=0.0, ge=0.0)
    hire_date: date
    phone: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class EmployeeUpdateIn(BaseModel):
    department: str | None = Field(default=None, min_length=1, max_length=64)
    position: str | None = Field(default=None, min_length=1, max_length=64)
    shift_group: Literal["A", "B", "C", "day"] | None = None
    base_salary: FiniteFloat | None = Field(default=None, ge=0.0)
    status: Literal["active", "on_leave", "resigned"] | None = None
    phone: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _emp_out(o: ObjectState) -> dict[str, Any]:
    s = o.state
    st = s.get("status") or "active"
    return {
        "id": o.object_id,
        "emp_no": s.get("emp_no") or o.object_id,
        "name": s.get("name") or o.object_id,
        "department": s.get("department") or "",
        "position": s.get("position") or "",
        "shift_group": s.get("shift_group") or "day",
        "base_salary": float(s.get("base_salary") or 0.0),
        "hire_date": s.get("hire_date"),
        "status": st,
        "status_text": EMP_STATUS_TEXT.get(st, st),
        "phone": s.get("phone"),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def employees_board(
    session: AsyncSession, tenant: str, *, status: str | None = None, mode: str = "real"
) -> dict[str, Any]:
    rows = await _scan(session, tenant, "employee", mode)
    items = [_emp_out(o) for o in rows]
    if status:
        items = [it for it in items if it["status"] == status]
    counts = {
        "total": len(items),
        "active": sum(1 for x in items if x["status"] == "active"),
        "on_leave": sum(1 for x in items if x["status"] == "on_leave"),
        "resigned": sum(1 for x in items if x["status"] == "resigned"),
    }
    return {"data_mode": mode, "counts": counts, "items": items}


async def register_employee(
    session: AsyncSession, principal: Principal, ledger: Ledger, body: EmployeeIn
) -> dict[str, Any]:
    eid = body.emp_no.strip()
    payload: dict[str, Any] = {
        "emp_no": eid,
        "name": body.name.strip(),
        "department": body.department.strip(),
        "position": body.position.strip(),
        "shift_group": body.shift_group,
        "base_salary": float(body.base_salary),
        "hire_date": body.hire_date.isoformat(),
        "status": "active",
    }
    if body.phone:
        payload["phone"] = body.phone.strip()
    if body.note:
        payload["note"] = body.note.strip()
    ev = body.evidence or [Evidence(kind="manual", ref=f"emp:{eid}", note="员工入职建档")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=EMP_REG_EVENT,
            object_id=eid,
            payload=payload,
            evidence=ev,
            expected_version=0,
            data_mode=body.data_mode,
        ),
    )
    return {"id": eid, "event": res.model_dump()}


async def update_employee(
    session: AsyncSession, principal: Principal, ledger: Ledger, emp_id: str, body: EmployeeUpdateIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "employee", emp_id)
    if o is None:
        raise HrNotFound(f"员工 {emp_id} 不存在")
    payload = body.model_dump(exclude_none=True, exclude={"evidence"})
    if not payload:
        raise HrError("至少需要提供一个更新字段", 422)
    ev = body.evidence or [Evidence(kind="manual", ref=f"emp:{emp_id}:upd", note="员工档案更新")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=EMP_UPD_EVENT,
            object_id=emp_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


# ---------------- 特种作业资质 ----------------


class WorkCertIn(BaseModel):
    cert_no: str = Field(min_length=1, max_length=64)
    employee: str = Field(min_length=1, max_length=64)
    kind: Literal["welder", "electrician", "forklift", "crane", "kiln_operator", "safety_officer", "other"]
    issued_date: date
    expires_date: date
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


class WorkCertRenewIn(BaseModel):
    expires_date: date
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


def _cert_out(o: ObjectState, emp_names: dict[str, str], today: date, remind_days: int) -> dict[str, Any]:
    s = o.state
    eid = s.get("employee") or ""
    kd = s.get("kind") or "other"
    st = s.get("status") or "valid"
    exp_str = s.get("expires_date")
    cert_state = "revoked" if st == "revoked" else "ok"
    if st != "revoked" and exp_str:
        with suppress(ValueError):
            d_exp = date.fromisoformat(str(exp_str))
            if d_exp < today:
                cert_state = "expired"
            elif d_exp <= today + timedelta(days=remind_days):
                cert_state = "expiring"
    return {
        "id": o.object_id,
        "cert_no": s.get("cert_no") or o.object_id,
        "employee": eid,
        "employee_name": emp_names.get(eid, eid),
        "kind": kd,
        "kind_text": CERT_KIND_TEXT.get(kd, kd),
        "issued_date": s.get("issued_date"),
        "expires_date": exp_str,
        "status": st,
        "cert_state": cert_state,
        "cert_state_text": CERT_STATE_TEXT.get(cert_state, cert_state),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def certs_board(
    session: AsyncSession, tenant: str, cfg: HrConfig, today: date, *, mode: str = "real"
) -> dict[str, Any]:
    emps = await _scan(session, tenant, "employee", mode)
    emp_names = {e.object_id: str(e.state.get("name") or e.object_id) for e in emps}
    rows = await _scan(session, tenant, "work_cert", mode)
    items = [_cert_out(o, emp_names, today, cfg.cert_remind_days) for o in rows]
    rank = {"expired": 0, "expiring": 1, "ok": 2, "revoked": 3}
    items.sort(key=lambda x: (rank.get(x["cert_state"], 9), x.get("expires_date") or "", x["id"]))
    counts = {
        "total": len(items),
        "expired": sum(1 for x in items if x["cert_state"] == "expired"),
        "expiring": sum(1 for x in items if x["cert_state"] == "expiring"),
        "ok": sum(1 for x in items if x["cert_state"] == "ok"),
    }
    return {"date": today.isoformat(), "data_mode": mode, "counts": counts, "items": items}


async def record_cert(
    session: AsyncSession, principal: Principal, ledger: Ledger, tz: ZoneInfo, body: WorkCertIn
) -> dict[str, Any]:
    cid = new_id("CERT", tz)
    payload: dict[str, Any] = {
        "cert_no": body.cert_no.strip(),
        "employee": body.employee.strip(),
        "kind": body.kind,
        "issued_date": body.issued_date.isoformat(),
        "expires_date": body.expires_date.isoformat(),
        "status": "valid",
    }
    if body.note:
        payload["note"] = body.note.strip()
    ev = body.evidence or [Evidence(kind="manual", ref=f"cert:{cid}", note="特种作业资质登记")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=CERT_REC_EVENT,
            object_id=cid,
            payload=payload,
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )
    return {"id": cid, "event": res.model_dump()}


async def renew_cert(
    session: AsyncSession, principal: Principal, ledger: Ledger, cert_id: str, body: WorkCertRenewIn
) -> AppendResult:
    o = await get(session, principal.tenant_id, "work_cert", cert_id)
    if o is None:
        raise HrNotFound(f"特种作业资质 {cert_id} 不存在")
    payload: dict[str, Any] = {"expires_date": body.expires_date.isoformat(), "status": "valid"}
    if body.note:
        payload["note"] = body.note.strip()
    ev = body.evidence or [Evidence(kind="manual", ref=f"cert:{cert_id}:renew", note="特种资质复审换证")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=CERT_RENEW_EVENT,
            object_id=cert_id,
            payload=payload,
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


async def remind_expiring_certs(
    session: AsyncSession, tenant: str, cfg: HrConfig, today: date, *, mode: str = "real"
) -> dict[str, Any]:
    """为已过期或临期的特种作业证照生成去重复审提醒待办。"""
    board = await certs_board(session, tenant, cfg, today, mode=mode)
    existing = {
        (t.object_id, t.due_at.date().isoformat() if t.due_at else "")
        for t in (
            await session.scalars(
                select(Todo).where(
                    Todo.tenant_id == tenant,
                    Todo.object_type == "work_cert",
                    Todo.status == "open",
                    Todo.data_mode == mode,
                )
            )
        ).all()
    }
    created: list[str] = []
    default_role = cfg.notify_roles[0] if cfg.notify_roles else "shift_lead"
    for it in board["items"]:
        if it["cert_state"] not in ("expired", "expiring"):
            continue
        exp_str = it["expires_date"] or ""
        if (it["id"], exp_str) in existing:
            continue
        due_dt = datetime.combine(date.fromisoformat(exp_str), time(17, 0), tzinfo=UTC) if exp_str else None
        prefix = "【证照已过期】" if it["cert_state"] == "expired" else "【证照临期】"
        session.add(
            Todo(
                tenant_id=tenant,
                title=f"{prefix}{it['employee_name']} {it['kind_text']}",
                body=f"证书号 {it['cert_no']}，到期日 {exp_str}，请安排复审并严禁无证上岗。",
                assignee_role=default_role,
                object_type="work_cert",
                object_id=it["id"],
                source="system:hr",
                created_by="system:hr",
                due_at=due_dt,
                data_mode=mode,
            )
        )
        created.append(it["id"])
    return {"date": today.isoformat(), "created": len(created), "cert_ids": created}


# ---------------- 排班与计件考勤 ----------------


class RosterIn(BaseModel):
    work_date: date
    shift_name: Literal["day", "night", "morning", "afternoon"] = "day"
    employee: str = Field(min_length=1, max_length=64)
    attendance: Literal["scheduled", "present", "late", "absent", "leave"] = "present"
    hours: FiniteFloat = Field(default=8.0, ge=0.0)
    overtime_hours: FiniteFloat = Field(default=0.0, ge=0.0)
    piece_qty: FiniteFloat = Field(default=0.0, ge=0.0)
    piece_rate: FiniteFloat | None = Field(default=None, ge=0.0)
    note: str | None = Field(default=None, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


def _roster_out(o: ObjectState, emp_names: dict[str, str]) -> dict[str, Any]:
    s = o.state
    eid = s.get("employee") or ""
    sh = s.get("shift_name") or "day"
    att = s.get("attendance") or "present"
    return {
        "id": o.object_id,
        "roster_no": s.get("roster_no") or o.object_id,
        "work_date": s.get("work_date"),
        "shift_name": sh,
        "shift_text": SHIFT_TEXT.get(sh, sh),
        "employee": eid,
        "employee_name": emp_names.get(eid, eid),
        "attendance": att,
        "attendance_text": ATTEND_TEXT.get(att, att),
        "hours": float(s.get("hours") or 0.0),
        "overtime_hours": float(s.get("overtime_hours") or 0.0),
        "piece_qty": float(s.get("piece_qty") or 0.0),
        "piece_rate": s.get("piece_rate"),
        "note": s.get("note"),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def roster_board(
    session: AsyncSession,
    tenant: str,
    *,
    month: str | None = None,
    employee: str | None = None,
    mode: str = "real",
) -> dict[str, Any]:
    emps = await _scan(session, tenant, "employee", mode)
    emp_names = {e.object_id: str(e.state.get("name") or e.object_id) for e in emps}
    rows = await _scan(session, tenant, "shift_roster", mode)
    items = [_roster_out(o, emp_names) for o in reversed(rows)]
    if month:
        items = [it for it in items if str(it.get("work_date") or "").startswith(f"{month}-")]
    if employee:
        items = [it for it in items if it["employee"] == employee]
    counts = {
        "total": len(items),
        "present": sum(1 for x in items if x["attendance"] in ("present", "late")),
        "absent": sum(1 for x in items if x["attendance"] == "absent"),
        "leave": sum(1 for x in items if x["attendance"] == "leave"),
        "total_hours": round(sum(x["hours"] for x in items), 2),
        "total_overtime": round(sum(x["overtime_hours"] for x in items), 2),
        "total_pieces": round(sum(x["piece_qty"] for x in items), 2),
    }
    return {"data_mode": mode, "counts": counts, "items": items}


async def log_roster(
    session: AsyncSession,
    principal: Principal,
    ledger: Ledger,
    tz: ZoneInfo,
    cfg: HrConfig,
    body: RosterIn,
) -> dict[str, Any]:
    rid = new_id("RST", tz)
    rate = cfg.default_piece_rate if body.piece_rate is None else float(body.piece_rate)
    payload: dict[str, Any] = {
        "roster_no": rid,
        "work_date": body.work_date.isoformat(),
        "shift_name": body.shift_name,
        "employee": body.employee.strip(),
        "attendance": body.attendance,
        "hours": float(body.hours),
        "overtime_hours": float(body.overtime_hours),
        "piece_qty": float(body.piece_qty),
        "piece_rate": rate,
    }
    if body.note:
        payload["note"] = body.note.strip()
    ev = body.evidence or [Evidence(kind="manual", ref=f"roster:{rid}", note="排班与计件考勤登记")]
    res = await ledger.append(
        session,
        principal,
        AppendRequest(
            type=ROSTER_LOG_EVENT,
            object_id=rid,
            payload=payload,
            evidence=ev,
            data_mode=body.data_mode,
        ),
    )
    return {"id": rid, "event": res.model_dump()}


# ---------------- 请假单 ----------------


class LeaveIn(BaseModel):
    employee: str = Field(min_length=1, max_length=64)
    kind: Literal["annual", "sick", "personal", "injury"] = "personal"
    start_date: date
    end_date: date
    days: FiniteFloat = Field(gt=0.0, le=365.0)
    reason: str = Field(min_length=1, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    data_mode: Literal["real", "mock"] = "real"


def _leave_out(o: ObjectState, emp_names: dict[str, str]) -> dict[str, Any]:
    s = o.state
    eid = s.get("employee") or ""
    kd = s.get("kind") or "personal"
    st = s.get("status") or "approved"
    return {
        "id": o.object_id,
        "leave_no": s.get("leave_no") or o.object_id,
        "employee": eid,
        "employee_name": emp_names.get(eid, eid),
        "kind": kd,
        "kind_text": LEAVE_KIND_TEXT.get(kd, kd),
        "start_date": s.get("start_date"),
        "end_date": s.get("end_date"),
        "days": float(s.get("days") or 0.0),
        "reason": s.get("reason") or "",
        "status": st,
        "status_text": LEAVE_STATUS_TEXT.get(st, st),
        "data_mode": o.data_mode,
        "version": o.version,
    }


async def leaves_board(session: AsyncSession, tenant: str, *, mode: str = "real") -> dict[str, Any]:
    emps = await _scan(session, tenant, "employee", mode)
    emp_names = {e.object_id: str(e.state.get("name") or e.object_id) for e in emps}
    rows = await _scan(session, tenant, "leave_request", mode)
    items = [_leave_out(o, emp_names) for o in reversed(rows)]
    counts = {
        "total": len(items),
        "approved": sum(1 for x in items if x["status"] == "approved"),
        "cancelled": sum(1 for x in items if x["status"] == "cancelled"),
    }
    return {"data_mode": mode, "counts": counts, "items": items}


def submit_leave_request(tz: ZoneInfo, body: LeaveIn) -> tuple[str, AppendRequest]:
    """构造请假提交请求（3 天及以上在路由层捕获 ApprovalRequired 转总经理审批）。"""
    lid = new_id("LV", tz)
    ev = body.evidence or [Evidence(kind="manual", ref=f"leave:{lid}", note="请假申请")]
    req = AppendRequest(
        type=LEAVE_SUB_EVENT,
        object_id=lid,
        payload={
            "leave_no": lid,
            "employee": body.employee.strip(),
            "kind": body.kind,
            "start_date": body.start_date.isoformat(),
            "end_date": body.end_date.isoformat(),
            "days": float(body.days),
            "reason": body.reason.strip(),
            "status": "approved",
        },
        evidence=ev,
        data_mode=body.data_mode,
    )
    return lid, req


async def cancel_leave(
    session: AsyncSession, principal: Principal, ledger: Ledger, leave_id: str
) -> AppendResult:
    o = await get(session, principal.tenant_id, "leave_request", leave_id)
    if o is None:
        raise HrNotFound(f"请假单 {leave_id} 不存在")
    ev = [Evidence(kind="manual", ref=f"leave:{leave_id}:cancel", note="撤销请假单")]
    return await ledger.append(
        session,
        principal,
        AppendRequest(
            type=LEAVE_CANCEL_EVENT,
            object_id=leave_id,
            payload={"status": "cancelled"},
            evidence=ev,
            expected_version=o.version,
            data_mode=o.data_mode,
        ),
    )


# ---------------- 月度薪酬核算 ----------------


async def payroll_report(
    session: AsyncSession, tenant: str, cfg: HrConfig, month: str, *, mode: str = "real"
) -> dict[str, Any]:
    emps = (await employees_board(session, tenant, mode=mode))["items"]
    rosters = (await roster_board(session, tenant, month=month, mode=mode))["items"]
    res = compute_payroll(cfg, month, emps, rosters)
    res["data_mode"] = mode
    return res
