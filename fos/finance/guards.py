"""财务账本守卫：注册在 Ledger.add_guard()，任何入口都绕不过去。

- 应收回款：严禁超收、严禁对已作废应收回款；
- 应付付款：严禁超付、严禁对已作废应付付款；
- 凭证草稿：强制复式记账借贷必相等且金额为有限正数，严禁不平凭证入账。
"""

from __future__ import annotations

import json
import math
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.governance.principal import Principal
from fos.ontology import Ontology

AR_RECORD_EVENT = "finance.ar_recorded"
AR_INVOICE_EVENT = "finance.ar_invoiced"
AR_RECEIVE_EVENT = "finance.ar_received"
AP_RECORD_EVENT = "finance.ap_recorded"
AP_PAY_EVENT = "finance.ap_paid"
VOUCHER_DRAFT_EVENT = "finance.voucher_drafted"
VOUCHER_EXPORT_EVENT = "finance.voucher_exported"
VOUCHER_POST_EVENT = "finance.voucher_posted"


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class FinanceGuard:
    """财务账本不变量守卫。"""

    def __init__(self, event_type: str):
        self.event_type = event_type

    async def __call__(
        self,
        session: AsyncSession,
        principal: Principal,
        onto: Ontology,
        payload: dict[str, Any],
        state: dict[str, Any] | None,
        *,
        object_id: str = "",
        data_mode: str = "real",
    ) -> list[str]:
        if self.event_type == AR_RECORD_EVENT:
            amt = _num(payload.get("amount"))
            rec = _num(payload.get("received_amount") or 0)
            if amt is None or amt <= 0:
                return ["应收金额必须为正数"]
            if rec is None or rec < 0 or rec > amt + 0.005:
                return ["已收金额不能为负且不能超过应收金额"]
            return []

        if self.event_type == AR_RECEIVE_EVENT:
            if state is None:
                return ["应收单不存在"]
            if state.get("status") == "void":
                return ["应收单已作废，不能登记回款"]
            amt = _num(state.get("amount")) or 0.0
            prev = _num(state.get("received_amount")) or 0.0
            new_rec = _num(payload.get("received_amount"))
            if new_rec is None or new_rec <= prev:
                return ["回款后的累计已收金额必须大于原已收金额"]
            if new_rec > amt + 0.005:
                return [f"累计回款 {new_rec:.2f} 超过应收金额 {amt:.2f}，禁止超收"]
            return []

        if self.event_type == AP_RECORD_EVENT:
            amt = _num(payload.get("amount"))
            paid = _num(payload.get("paid_amount") or 0)
            if amt is None or amt <= 0:
                return ["应付金额必须为正数"]
            if paid is None or paid < 0 or paid > amt + 0.005:
                return ["已付金额不能为负且不能超过应付金额"]
            return []

        if self.event_type == AP_PAY_EVENT:
            if state is None:
                return ["应付单不存在"]
            if state.get("status") == "void":
                return ["应付单已作废，不能登记付款"]
            amt = _num(state.get("amount")) or 0.0
            prev = _num(state.get("paid_amount")) or 0.0
            new_paid = _num(payload.get("paid_amount"))
            if new_paid is None or new_paid <= prev:
                return ["付款后的累计已付金额必须大于原已付金额"]
            if new_paid > amt + 0.005:
                return [f"累计付款 {new_paid:.2f} 超过应付金额 {amt:.2f}，禁止超付"]
            return []

        if self.event_type == VOUCHER_DRAFT_EVENT:
            return _check_voucher_balance(payload)

        if self.event_type == VOUCHER_POST_EVENT:
            if state is None:
                return ["凭证草稿不存在"]
            if state.get("status") == "void":
                return ["已作废凭证不能过账"]
            return []

        return []


def _check_voucher_balance(payload: dict[str, Any]) -> list[str]:
    lines = payload.get("lines")
    if isinstance(lines, str):
        try:
            lines = json.loads(lines)
        except json.JSONDecodeError:
            return ["凭证分录 JSON 格式非法"]
    if not isinstance(lines, list) or len(lines) < 2:
        return ["凭证分录至少需要 2 行（有借必有贷）"]
    debit_sum = 0.0
    credit_sum = 0.0
    for idx, line in enumerate(lines, 1):
        if not isinstance(line, dict):
            return [f"第 {idx} 行分录格式非法"]
        if not str(line.get("account_code") or "").strip():
            return [f"第 {idx} 行分录缺少科目编码"]
        d = _num(line.get("debit") or 0)
        c = _num(line.get("credit") or 0)
        if d is None or c is None or d < 0 or c < 0:
            return [f"第 {idx} 行分录借贷金额必须为非负有限数字"]
        if d > 0 and c > 0:
            return [f"第 {idx} 行分录不能同时填借方和贷方"]
        debit_sum += d
        credit_sum += c
    if debit_sum <= 0 or abs(debit_sum - credit_sum) >= 0.005:
        return [f"凭证借贷不平：借方 {debit_sum:.2f} ≠ 贷方 {credit_sum:.2f}"]
    td = _num(payload.get("total_debit"))
    tc = _num(payload.get("total_credit"))
    if td is None or tc is None or abs(td - debit_sum) >= 0.01 or abs(tc - credit_sum) >= 0.01:
        return ["凭证借贷合计与分录明细不一致"]
    return []
