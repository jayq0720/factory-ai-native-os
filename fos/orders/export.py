"""发货单 Excel：由账本里的发货、订单、批次和出厂检验组装，不手抄。模拟数据不出正式文件（规则 4）。"""

from __future__ import annotations

import io
from typing import Any

STATUS_TEXT = {"in_transit": "在途", "delivered": "已签收", "refused": "客户拒收"}


def shipment_xlsx(factory: str, info: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "发货单"
    ws.append([f"{factory} 发货单"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    for k, v in [
        ("发货单号", info["shipment_no"]),
        ("客户", info["customer"]),
        ("交货地点", info.get("delivery_address") or ""),
        ("合同编号", info.get("contract_no") or ""),
        ("订单号", info["order_no"]),
        ("发货时间", info["shipped_at"]),
        ("承运方", info.get("carrier") or ""),
        ("车牌号", info.get("vehicle_no") or ""),
        ("状态", STATUS_TEXT.get(info["status"], info["status"])),
    ]:
        ws.append([k, v])
    ws.append([])
    ws.append(["牌号", "产品名称", "规格", "数量", "单位"])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    ws.append(
        [
            info["product_code"],
            info["product_name"],
            info.get("product_spec") or "",
            info["qty"],
            info["unit"],
        ]
    )
    ws.append([])
    ws.append(["批号", "合格数量", "单位", "出厂检验", "检验报告编号"])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for b in info["batches"]:
        ws.append(
            [b["id"], b.get("good_qty"), b.get("unit"), b.get("final_inspection"), b.get("report_no") or ""]
        )
    ws.append([])
    if info.get("note"):
        ws.append(["备注", info["note"]])
    ws.append(["发货人", "", "", "收货人签字", ""])
    ws.append(["", "", "", "签收日期", ""])
    for col, w in zip("ABCDE", (14, 22, 14, 14, 18), strict=True):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
