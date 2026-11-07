"""方案导出：技术方案（Word）、BOM + 报价 + 审核（Excel）。模拟数据不能导出正式文件（由路由检查）。

技术方案正文由模板拼装（工况 → 选材理由 → 衬里结构 → 传热校核 → 施工要点 → 执行标准），
数字全部来自确定性计算结果；以后接入模型润色时，只改写文字，不改表格里的数字。
"""

from __future__ import annotations

import io
from typing import Any

from fos.engineering.review import DISPOSITIONS

SEVERITY = {"high": "高", "medium": "中", "low": "低"}


def proposal_sections(project: dict[str, Any], design: dict[str, Any], label: str) -> list[tuple[str, list]]:
    """技术方案的章节：[(标题, [段落 str 或 表格 list[list]])]。"""
    d = design
    c = d["conditions"]
    hn, hm = d.get("heat_normal") or {}, d.get("heat_max") or {}
    cond_rows = [
        ["项目", "取值"],
        ["炉型 / 部位", f"{c.get('furnace_type') or '—'} / {'、'.join(c.get('positions') or []) or '—'}"],
        ["正常工作温度 / 最高温度", f"{c['normal_temp']:g} ℃ / {c['max_temp']:g} ℃"],
        ["气氛", "、".join(c.get("atmosphere") or []) or "—"],
        ["外壁温度要求 / 环境温度", f"≤ {c['shell_temp']:g} ℃ / {c['ambient']:g} ℃"],
        ["衬里面积", f"{c['area']:g} m²"],
    ]
    layer_rows = [["层", "产品", "厚度 (mm)", "热面温度 (℃，最高工况)"]]
    temps = hm.get("interface_temps") or []
    for i, ly in enumerate(d["layers"]):
        t = f"{temps[i]:.0f}" if i < len(temps) else "—"
        layer_rows.append([ly["role"], f"{ly['code']} {ly['name']}", f"{ly['thickness_mm']:g}", t])
    layer_rows.append(["合计", "", f"{d['total_thickness_mm']:g}", ""])
    calc = [
        f"正常工况：热流 {hn.get('heat_flux', '—')} W/m²，外壁温度 {hn.get('shell_temp', '—')} ℃。",
        f"最高温度工况：热流 {hm.get('heat_flux', '—')} W/m²，外壁温度 {hm.get('shell_temp', '—')} ℃。",
        "计算方法：多层平壁一维稳态传热，导热系数 k(T) = a + b·T，外表面自然对流 + 辐射换热。",
        *[("✔ " if ch["ok"] else "✘ ") + ch["text"] for ch in d.get("checks", [])],
    ]
    sections: list[tuple[str, list]] = [
        (
            "一、工程概况与工况",
            [f"项目：{project.get('name', '')}；客户：{project.get('customer_name') or '—'}。", cond_rows],
        ),
        ("二、选材理由", list(d.get("reasons") or [])),
        ("三、衬里结构", [layer_rows]),
        ("四、传热校核", calc),
        ("五、施工要点", list(d.get("notes") or []) or ["按客户图纸与执行标准施工。"]),
        ("六、执行标准", [project.get("standards") or "待与客户确认。"]),
    ]
    if c.get("assumptions"):
        sections.append(("七、计算假设", [a["text"] for a in c["assumptions"]]))
    return sections


def to_docx(project: dict[str, Any], design: dict[str, Any], label: str, version: int) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading(f"{project.get('name', '')} 技术方案（{label} 第 {version} 版）", level=1)
    for title, blocks in proposal_sections(project, design, label):
        doc.add_heading(title, level=2)
        for b in blocks:
            if isinstance(b, list):
                t = doc.add_table(rows=len(b), cols=len(b[0]))
                t.style = "Table Grid"
                for r, row in enumerate(b):
                    for col, v in enumerate(row):
                        t.cell(r, col).text = str(v)
            else:
                doc.add_paragraph(str(b))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def to_xlsx(
    project: dict[str, Any], design: dict[str, Any], review: list[dict[str, Any]], label: str, version: int
) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "BOM"
    ws.append([f"{project.get('name', '')} 材料清单（{label} 第 {version} 版）"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(
        [
            "序号",
            "层",
            "牌号",
            "名称",
            "规格",
            "厚度 mm",
            "面积 m²",
            "净体积 m³",
            "损耗 %",
            "体积 m³",
            "重量 t",
            "数量",
            "单位",
            "计算式",
        ]
    )
    for b in design["bom"]:
        ws.append(
            [
                b["line"],
                b["role"],
                b["code"],
                b["name"],
                b.get("spec"),
                b.get("thickness_mm"),
                b.get("area_m2"),
                b.get("net_volume_m3"),
                b.get("loss_rate"),
                b.get("volume_m3"),
                b.get("weight_t"),
                b.get("qty"),
                b.get("unit"),
                b.get("formula"),
            ]
        )

    q = design["quote"]
    wq = wb.create_sheet("报价")
    wq.append(["序号", "牌号", "名称", "数量", "单位", "单价（元）", "金额（元）", "价格版本"])
    for ln in q["lines"]:
        wq.append(
            [
                ln["line"],
                ln["code"],
                ln["name"],
                ln["qty"],
                ln["unit"],
                ln.get("unit_price"),
                ln.get("amount"),
                ln.get("price_ref"),
            ]
        )
    wq.append([])
    o = q["options"]
    for k, v in [
        ("材料金额", q["material_list"]),
        (f"折扣 {o['discount_pct']:g}%", -q["discount"]),
        ("材料净价", q["material_net"]),
        (f"运费（{q['total_weight_t']:g} t × {o['freight_per_t']:g} 元/t）", q["freight"]),
        (f"施工费（{o['install_per_m2']:g} 元/m²）", q["install"]),
        ("不含税小计", q["subtotal"]),
        (f"税额 {o['tax_rate']:g}%", q["tax"]),
        ("含税合计", q["total"]),
    ]:
        wq.append(["", k, "", "", "", "", v])
    wq.append([])
    for f in q.get("formulas", []):
        wq.append(["", f])

    wr = wb.create_sheet("审核")
    wr.append(["级别", "类型", "问题", "建议", "处理", "处理人", "说明"])
    for it in review:
        wr.append(
            [
                SEVERITY.get(it["severity"], it["severity"]),
                it["type"],
                it["message"],
                it["suggestion"],
                DISPOSITIONS.get(it.get("disposition") or "", "未处理"),
                it.get("by_name", ""),
                it.get("note", ""),
            ]
        )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
