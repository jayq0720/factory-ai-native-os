"""确定性商机漏斗分析与跨部门经营决策驾驶舱 KPI 汇总（规则 3：绝不让模型算金额与转化率）。"""

from __future__ import annotations

import math
from typing import Any


def _finite_nonneg(val: Any, label: str) -> float:
    if val is None:
        return 0.0
    if isinstance(val, bool) or not isinstance(val, int | float) or not math.isfinite(val):
        raise ValueError(f"{label}必须为有限数字")
    f = float(val)
    if f < 0:
        raise ValueError(f"{label}不能为负数")
    return f


def compute_funnel(leads: list[dict[str, Any]]) -> dict[str, Any]:
    """确定性计算商机漏斗总金额、加权预测额、赢单额、赢单率与分阶段统计。"""
    stages = ("new", "qualified", "quoted", "negotiation", "won", "lost")
    by_stage: dict[str, dict[str, Any]] = {s: {"count": 0, "amount": 0.0, "weighted": 0.0} for s in stages}
    raw_open = 0.0
    weighted_open = 0.0
    won_amt = 0.0
    open_cnt = won_cnt = lost_cnt = 0

    for ld in leads:
        st = str(ld.get("stage") or "new")
        amt = _finite_nonneg(ld.get("est_amount"), "商机预计金额")
        prob = _finite_nonneg(ld.get("probability"), "赢单概率")
        if prob > 100.0:
            raise ValueError("赢单概率不能超过 100")
        w_amt = amt * (prob / 100.0)
        if st in by_stage:
            by_stage[st]["count"] += 1
            by_stage[st]["amount"] = round(by_stage[st]["amount"] + amt, 2)
            by_stage[st]["weighted"] = round(by_stage[st]["weighted"] + w_amt, 2)
        if st == "won":
            won_cnt += 1
            won_amt += amt
        elif st == "lost":
            lost_cnt += 1
        else:
            open_cnt += 1
            raw_open += amt
            weighted_open += w_amt

    closed = won_cnt + lost_cnt
    win_rate = round(won_cnt / closed * 100.0, 2) if closed > 0 else 0.0
    return {
        "total": len(leads),
        "open_count": open_cnt,
        "won_count": won_cnt,
        "lost_count": lost_cnt,
        "raw_pipeline_amount": round(raw_open, 2),
        "weighted_pipeline_amount": round(weighted_open, 2),
        "won_amount": round(won_amt, 2),
        "win_rate_pct": win_rate,
        "by_stage": by_stage,
    }


def compute_cockpit_kpis(
    *,
    funnel: dict[str, Any],
    receivables: list[dict[str, Any]],
    payables: list[dict[str, Any]],
    ncrs: list[dict[str, Any]],
    work_orders: list[dict[str, Any]],
    hazards: list[dict[str, Any]],
    employees: list[dict[str, Any]],
    rnd_projects: list[dict[str, Any]],
    formulas: list[dict[str, Any]],
    sites: list[dict[str, Any]],
) -> dict[str, Any]:
    """跨部门确定性经营决策总览指标。"""
    ar_out = round(
        sum(
            max(0.0, float(r.get("amount") or 0.0) - float(r.get("received_amount") or 0.0))
            for r in receivables
            if r.get("status") != "paid"
        ),
        2,
    )
    ap_out = round(
        sum(
            max(0.0, float(p.get("amount") or 0.0) - float(p.get("paid_amount") or 0.0))
            for p in payables
            if p.get("status") != "paid"
        ),
        2,
    )
    return {
        "market_open_leads": funnel["open_count"],
        "market_weighted_pipeline": funnel["weighted_pipeline_amount"],
        "market_won_amount": funnel["won_amount"],
        "market_win_rate_pct": funnel["win_rate_pct"],
        "finance_ar_outstanding": ar_out,
        "finance_ap_outstanding": ap_out,
        "quality_open_ncr": sum(1 for n in ncrs if n.get("status") != "closed"),
        "eam_open_wo": sum(1 for w in work_orders if w.get("status") in ("open", "in_progress")),
        "ehs_open_hazards": sum(1 for h in hazards if h.get("status") != "verified"),
        "ehs_major_open_hazards": sum(
            1 for h in hazards if h.get("level") == "major" and h.get("status") != "verified"
        ),
        "hr_active_headcount": sum(1 for e in employees if e.get("status") == "active"),
        "rnd_active_projects": sum(1 for p in rnd_projects if p.get("status") in ("active", "trial")),
        "rnd_approved_formulas": sum(1 for f in formulas if f.get("status") == "approved"),
        "construction_active_sites": sum(
            1 for s in sites if s.get("status") in ("mobilizing", "building", "drying")
        ),
    }
