"""财务凭证草稿（金蝶 / 用友 / 通用）与往来对账单 Excel 导出。"""

from __future__ import annotations

import io
from typing import Any

from fos.excel_safe import excel_row


def vouchers_export_xlsx(factory: str, vouchers: list[dict[str, Any]], *, target: str = "generic") -> bytes:
    """导出借贷平衡的凭证草稿表（适配金蝶云星空 / 用友 U8 / 通用导入格式，防公式注入）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "凭证导入"
    if target == "kingdee":
        headers = [
            "凭证日期",
            "凭证字",
            "凭证号",
            "摘要",
            "科目编码",
            "科目名称",
            "币别",
            "借方金额",
            "贷方金额",
            "来源单号",
        ]
    elif target == "yonyou":
        headers = [
            "制单日期",
            "凭证类别",
            "凭证编号",
            "摘要",
            "科目编码",
            "科目名称",
            "借方本币",
            "贷方本币",
            "附单据号",
        ]
    else:
        headers = [
            "凭证日期",
            "凭证草稿号",
            "来源类型",
            "来源单号",
            "摘要",
            "科目编码",
            "科目名称",
            "借方金额",
            "贷方金额",
            "状态",
        ]

    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for v in vouchers:
        for line in v.get("lines") or []:
            memo = line.get("memo") or v.get("summary") or ""
            debit = round(float(line.get("debit") or 0.0), 2)
            credit = round(float(line.get("credit") or 0.0), 2)
            if target == "kingdee":
                row = [
                    v.get("voucher_date") or "",
                    "记",
                    v.get("voucher_no") or "",
                    memo,
                    line.get("account_code") or "",
                    line.get("account_name") or "",
                    "RMB",
                    debit,
                    credit,
                    v.get("source_id") or "",
                ]
            elif target == "yonyou":
                row = [
                    v.get("voucher_date") or "",
                    "记",
                    v.get("voucher_no") or "",
                    memo,
                    line.get("account_code") or "",
                    line.get("account_name") or "",
                    debit,
                    credit,
                    v.get("source_id") or "",
                ]
            else:
                row = [
                    v.get("voucher_date") or "",
                    v.get("voucher_no") or "",
                    v.get("source_type_text") or "",
                    v.get("source_id") or "",
                    memo,
                    line.get("account_code") or "",
                    line.get("account_name") or "",
                    debit,
                    credit,
                    v.get("status_text") or "",
                ]
            ws.append(excel_row(row))

    ws_info = wb.create_sheet("导出说明")
    ws_info.append(excel_row(["单位", factory]))
    ws_info.append(excel_row(["目标格式", target]))
    ws_info.append(excel_row(["凭证张数", len(vouchers)]))
    ws_info.append(excel_row(["说明", "仅含真实业务凭证草稿；导入财务软件后由财务人员审核过账。"]))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def reconciliation_xlsx(factory: str, statement: dict[str, Any]) -> bytes:
    """导出客户/供应商正式往来对账单（.xlsx）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    kind_text = "客户应收对账单" if statement["party_type"] == "customer" else "供应商应付对账单"
    ws.title = "往来对账单"
    ws.append(excel_row([f"{factory} {kind_text}"]))
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(excel_row(["往来单位", statement["party_name"], "对账日期", statement["date"]]))
    ws.append(
        excel_row(
            [
                "挂账总额（元）",
                statement["total_amount"],
                "已结清（元）",
                statement["total_settled"],
                "未结余额（元）",
                statement["total_balance"],
            ]
        )
    )
    ws.append([])
    is_cust = statement["party_type"] == "customer"
    settled_col = "已收金额" if is_cust else "已付金额"
    cols = [
        "单号",
        "关联单据",
        "到期日",
        "含税金额",
        settled_col,
        "未结余额",
        "逾期天数",
        "发票号",
        "状态",
    ]
    ws.append(cols)
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for it in statement.get("items") or []:
        ref = it.get("order") or it.get("purchase_order") or ""
        settled = it.get("received_amount") if is_cust else it.get("paid_amount")
        ws.append(
            excel_row(
                [
                    it.get("ar_no") or it.get("ap_no") or it["id"],
                    ref,
                    it.get("due_date") or "",
                    it["amount"],
                    settled,
                    it["balance"],
                    it["overdue_days"],
                    it.get("invoice_no") or "",
                    it.get("status_text") or "",
                ]
            )
        )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
