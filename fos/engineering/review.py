"""审核：风险与缺失信息清单（方案 4.5）。

每条提示带出处（原文页码或计算依据）和建议；工程师逐条标“已处理 / 接受风险 / 不适用”后才能发布。
blocking 的条目（模拟数据、缺价格、缺热工参数、低于成本）不能标记放过，只能修正数据后重新计算让它消失。
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from fos.engineering.config import EngineeringConfig

DISPOSITIONS = {"resolved": "已处理", "accepted": "接受风险", "na": "不适用"}
# 工程项目必须有的工况：缺了会进入审核清单
REQUIRED = {
    "max_temp": "最高温度",
    "normal_temp": "正常工作温度",
    "atmosphere": "气氛",
    "shell_temp": "外壁温度要求",
    "area": "衬里面积",
    "standards": "执行标准",
}
CHECK_TYPE = {
    "hot_face_margin": "material",
    "shell_temp": "thermal",
    "thickness": "thermal",
    "heat": "thermal",
    "thermal_data": "material",
}
BLOCKING_PREFIX = ("price_", "thermal_data", "unknown_product", "unit_")


def _id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def _item(
    kind: str,
    key: str,
    severity: str,
    message: str,
    suggestion: str,
    source: dict[str, Any] | None = None,
    *,
    blocking: bool = False,
) -> dict[str, Any]:
    return {
        "id": _id(kind, key, message),
        "type": kind,
        "key": key,
        "severity": severity,
        "message": message,
        "suggestion": suggestion,
        "source": source,
        "blocking": blocking,
        "disposition": None,
    }


def build_review(
    project: dict[str, Any],
    design: dict[str, Any],
    doc_fields: list[dict[str, Any]],
    cfg: EngineeringConfig,
    *,
    today: date | None = None,
    previous: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    today = today or date.today()
    items: list[dict[str, Any]] = []
    by_key = {f["key"]: f for f in doc_fields}

    # ---------- 数据模式 ----------
    if design.get("data_mode") == "mock":
        items.append(
            _item(
                "data",
                "mock",
                "high",
                "方案用到了模拟数据（模拟识别结果或演示知识库），不能发布、不能导出正式文件",
                "接入真实 OCR / 维护真实知识库后重新生成方案",
                blocking=True,
            )
        )

    # ---------- 缺失 ----------
    for key, name in REQUIRED.items():
        if project.get(key) in (None, ""):
            items.append(
                _item(
                    "missing",
                    key,
                    "high" if key != "standards" else "medium",
                    f"文件没有给出{name}",
                    f"向客户确认{name}后在工程项目里补充",
                )
            )
    for a in design.get("conditions", {}).get("assumptions", []):
        items.append(_item("missing", f"assume_{a['key']}", "medium", a["text"], "确认该假设是否成立"))
    if not project.get("quote_validity"):
        items.append(
            _item("commercial", "quote_validity", "low", "文件没有给出报价有效期", "按公司默认有效期填写")
        )

    # ---------- 冲突、未核对（来自文件字段） ----------
    for f in doc_fields:
        if f.get("conflict"):
            pages = sorted(
                {s.get("page") for c in f.get("candidates") or [] for s in c.get("sources") or []} - {None}
            )
            values = "、".join(dict.fromkeys(str(c.get("value")) for c in f.get("candidates") or []))
            items.append(
                _item(
                    "conflict",
                    f["key"],
                    "high",
                    f"{f['name']} 不同页给出的值不一致：{values}（第 {', '.join(map(str, pages))} 页）",
                    "与客户确认以哪一处为准，在字段核对里改正",
                    {"document_id": f.get("document_id"), "pages": pages},
                )
            )
    unchecked = [
        f for f in doc_fields if f.get("value") and f.get("status") == "extracted" and f.get("required")
    ]
    for f in unchecked:
        src = (f.get("sources") or [{}])[0]
        items.append(
            _item(
                "unchecked",
                f["key"],
                "low",
                f"{f['name']}「{f['value']}」是自动识别结果，还没有人工核对",
                "在字段核对页确认",
                {"document_id": f.get("document_id"), "page": src.get("page"), "quote": src.get("quote")},
            )
        )
    shell_field = by_key.get("shell_temp")

    # ---------- 选材与传热（计算依据） ----------
    for c in design.get("checks", []):
        if c["ok"]:
            continue
        kind = CHECK_TYPE.get(c["key"], "commercial" if c["key"].startswith("price_") else "material")
        source: dict[str, Any] = {"calculation": True}
        if c["key"] == "shell_temp" and shell_field and shell_field.get("sources"):
            s = shell_field["sources"][0]
            source = {
                "calculation": True,
                "document_id": shell_field.get("document_id"),
                "page": s.get("page"),
                "quote": s.get("quote"),
            }
        items.append(
            _item(
                kind,
                c["key"],
                c["severity"],
                c["text"],
                _suggest(c["key"]),
                source,
                blocking=c["key"].startswith(BLOCKING_PREFIX),
            )
        )
    # ---------- 商务 ----------
    q = design.get("quote", {})
    for line in q.get("lines", []):
        vu = line.get("price_valid_until")
        if vu and date.fromisoformat(vu) < today:
            items.append(
                _item(
                    "commercial",
                    f"price_expired_{line['product_id']}",
                    "high",
                    f"{line['code']} 的价格（{line.get('price_ref')}）已于 {vu} 过期",
                    "由知识库管理员更新价格后重新计算",
                    {"price_ref": line.get("price_ref")},
                )
            )
    margin = q.get("gross_margin_pct")
    if margin is None and q.get("lines"):
        items.append(
            _item(
                "commercial",
                "margin_unknown",
                "medium",
                "部分产品没有成本价，无法计算毛利率",
                "请知识库管理员补充成本价",
                blocking=True,
            )
        )
    elif margin is not None and margin < 0:
        items.append(
            _item(
                "commercial",
                "below_cost",
                "high",
                f"材料毛利率 {margin:g}%，报价低于材料成本",
                "调整折扣或价格；低于成本的报价不能发布",
                blocking=True,
            )
        )
    elif margin is not None and margin < cfg.quote.low_margin_pct:
        items.append(
            _item(
                "commercial",
                "low_margin",
                "medium",
                f"材料毛利率 {margin:g}%，低于 {cfg.quote.low_margin_pct:g}%（发布时需要总经理审批）",
                "确认是否接受低毛利",
            )
        )
    if q.get("options", {}).get("install_per_m2", 0) == 0:
        items.append(
            _item(
                "commercial",
                "supply_only",
                "low",
                "报价只含供料，不含施工费",
                "如客户要求包工包料，在报价选项里填写施工单价",
            )
        )

    # 保留上一版本对相同条目的处理结果
    prev = {p["id"]: p for p in previous or []}
    for it in items:
        old = prev.get(it["id"])
        if old and old.get("disposition") and not it["blocking"]:
            for k in ("disposition", "by", "by_name", "at", "note"):
                if k in old:
                    it[k] = old[k]
    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda x: (order[x["severity"]], x["type"]))
    return items


def _suggest(key: str) -> str:
    if key == "hot_face_margin":
        return "换分类温度更高的工作层产品"
    if key == "shell_temp":
        return "加厚隔热层或换导热系数更低的材料"
    if key == "thickness":
        return "调整各层厚度，或与客户确认厚度要求"
    if key.startswith("layer"):
        return "该层热面温度过高：加厚上一层或换耐温更高的产品"
    if key.startswith("price_"):
        return "由知识库管理员补充价格"
    if key.startswith("atmosphere_"):
        return "换适用该气氛的产品"
    return "核实后处理"


def summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    open_items = [i for i in items if not i.get("disposition")]
    blocking = [i for i in items if i["blocking"]]
    return {
        "total": len(items),
        "open": len(open_items),
        "high_open": sum(i["severity"] == "high" for i in open_items),
        "blocking": len(blocking),
        "ready": not open_items and not blocking,
    }
