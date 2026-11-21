"""人力、排班、计件与特种资质四表 Excel 导出（防公式注入、仅真实数据）。"""

from __future__ import annotations

import io
from typing import Any

from fos.excel_safe import excel_row


def hr_report_xlsx(
    factory: str,
    month: str,
    employees: list[dict[str, Any]],
    certs: list[dict[str, Any]],
    rosters: list[dict[str, Any]],
    payroll: dict[str, Any],
) -> bytes:
    """导出员工花名册、特种资质台账、排班考勤与计件、月度薪酬核算四表 Excel。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    w_emp = wb.active
    w_emp.title = "员工花名册"
    w_emp.append(excel_row([f"{factory} 员工花名册（{month}）"]))
    w_emp["A1"].font = Font(bold=True, size=13)
    w_emp.append(["工号", "姓名", "部门", "岗位", "班组", "月基本工资", "入职日期", "状态"])
    for c in w_emp[w_emp.max_row]:
        c.font = Font(bold=True)
    for e in employees:
        w_emp.append(
            excel_row(
                [
                    e["emp_no"],
                    e["name"],
                    e["department"],
                    e["position"],
                    e["shift_group"],
                    e["base_salary"],
                    e.get("hire_date") or "",
                    e["status_text"],
                ]
            )
        )

    w_cert = wb.create_sheet("特种资质台账")
    w_cert.append(["证书编号", "员工", "资质类型", "发证日期", "到期日期", "状态", "备注"])
    for c in w_cert[1]:
        c.font = Font(bold=True)
    for ct in certs:
        w_cert.append(
            excel_row(
                [
                    ct["cert_no"],
                    ct["employee_name"],
                    ct["kind_text"],
                    ct.get("issued_date") or "",
                    ct.get("expires_date") or "",
                    ct["cert_state_text"],
                    ct.get("note") or "",
                ]
            )
        )

    w_rst = wb.create_sheet("排班考勤与计件")
    w_rst.append(["单号", "日期", "班次", "员工", "出勤", "工时", "加班(h)", "计件产量", "单价"])
    for c in w_rst[1]:
        c.font = Font(bold=True)
    for r in rosters:
        w_rst.append(
            excel_row(
                [
                    r["roster_no"],
                    r.get("work_date") or "",
                    r["shift_text"],
                    r["employee_name"],
                    r["attendance_text"],
                    r["hours"],
                    r["overtime_hours"],
                    r["piece_qty"],
                    r.get("piece_rate") if r.get("piece_rate") is not None else "",
                ]
            )
        )

    w_pay = wb.create_sheet("月度薪酬核算")
    pay_cols = ["工号", "姓名", "部门", "出勤天", "加班(h)", "计件量", "基本折算", "加班费", "计件", "应发"]
    w_pay.append(pay_cols)
    for c in w_pay[1]:
        c.font = Font(bold=True)
    for p in payroll.get("items") or []:
        w_pay.append(
            excel_row(
                [
                    p["emp_no"],
                    p["name"],
                    p["department"],
                    p["present_days"],
                    p["overtime_hours"],
                    p["piece_qty"],
                    p["base_pay"],
                    p["overtime_pay"],
                    p["piece_pay"],
                    p["gross_pay"],
                ]
            )
        )

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
