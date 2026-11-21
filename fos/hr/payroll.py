"""确定性月度出勤与计件薪酬核算（规则 3：绝不让模型算工资与出勤）。"""

from __future__ import annotations

import math
from typing import Any

from fos.hr.config import HrConfig


def _finite_nonneg(val: Any, label: str) -> float:
    if val is None:
        return 0.0
    if isinstance(val, bool) or not isinstance(val, int | float) or not math.isfinite(val):
        raise ValueError(f"{label}必须为有限数字")
    f = float(val)
    if f < 0:
        raise ValueError(f"{label}不能为负数")
    return f


def compute_payroll(
    cfg: HrConfig,
    month: str,
    employees: list[dict[str, Any]],
    rosters: list[dict[str, Any]],
) -> dict[str, Any]:
    """按月份（YYYY-MM）确定性计算每位员工出勤、加班、计件与应发工资。"""
    prefix = f"{month}-"
    by_emp: dict[str, list[dict[str, Any]]] = {}
    for r in rosters:
        wd = str(r.get("work_date") or "")
        if not wd.startswith(prefix):
            continue
        eid = str(r.get("employee") or "")
        if eid:
            by_emp.setdefault(eid, []).append(r)

    items: list[dict[str, Any]] = []
    tot_base = tot_ot = tot_piece = tot_gross = 0.0
    for emp in employees:
        eid = emp["id"]
        recs = by_emp.get(eid, [])
        if emp.get("status") == "resigned" and not recs:
            continue
        base_sal = _finite_nonneg(emp.get("base_salary"), "月基本工资")
        present_days = sum(1 for r in recs if r.get("attendance") in ("present", "late"))
        absent_days = sum(1 for r in recs if r.get("attendance") == "absent")
        leave_days = sum(1 for r in recs if r.get("attendance") == "leave")
        total_hours = sum(_finite_nonneg(r.get("hours"), "出勤工时") for r in recs)
        ot_hours = sum(_finite_nonneg(r.get("overtime_hours"), "加班工时") for r in recs)
        piece_qty = 0.0
        piece_pay = 0.0
        for r in recs:
            qty = _finite_nonneg(r.get("piece_qty"), "计件产量")
            rate_raw = r.get("piece_rate")
            rate = cfg.default_piece_rate if rate_raw is None else _finite_nonneg(rate_raw, "计件单价")
            piece_qty += qty
            piece_pay += qty * rate
        day_ratio = min(present_days, cfg.standard_work_days) / float(cfg.standard_work_days)
        base_pay = round(base_sal * day_ratio, 2)
        hourly = base_sal / (cfg.standard_work_days * 8.0) if base_sal > 0 else 0.0
        ot_pay = round(ot_hours * hourly * cfg.overtime_multiplier, 2)
        piece_pay = round(piece_pay, 2)
        gross_pay = round(base_pay + ot_pay + piece_pay, 2)
        tot_base += base_pay
        tot_ot += ot_pay
        tot_piece += piece_pay
        tot_gross += gross_pay
        items.append(
            {
                "employee_id": eid,
                "emp_no": emp.get("emp_no") or eid,
                "name": emp.get("name") or eid,
                "department": emp.get("department") or "",
                "position": emp.get("position") or "",
                "present_days": present_days,
                "absent_days": absent_days,
                "leave_days": leave_days,
                "total_hours": round(total_hours, 2),
                "overtime_hours": round(ot_hours, 2),
                "piece_qty": round(piece_qty, 2),
                "base_pay": base_pay,
                "overtime_pay": ot_pay,
                "piece_pay": piece_pay,
                "gross_pay": gross_pay,
            }
        )

    return {
        "month": month,
        "standard_work_days": cfg.standard_work_days,
        "totals": {
            "headcount": len(items),
            "base_pay": round(tot_base, 2),
            "overtime_pay": round(tot_ot, 2),
            "piece_pay": round(tot_piece, 2),
            "gross_pay": round(tot_gross, 2),
        },
        "items": items,
    }
