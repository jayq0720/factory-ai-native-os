"""采购员 Skill 用的只读工具：实时库存、请购建议、比价、供应商评分（数字全部由确定性代码算）。

起草请购单走 ledger.propose（出卡片，采购员点确认后才入账）。
"""

from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from fos.db.models import utcnow
from fos.orders.config import OrdersConfig
from fos.orders.data import find_by
from fos.supply.purchase import SupplyError, compare_quotes, suggestions
from fos.supply.stock import material_stock
from fos.supply.suppliers import FORMULA, score_suppliers
from fos.tools.registry import ToolContext, ToolError, tool


def _env(ctx: ToolContext):
    cfg = ctx.extras.get("supply")
    if cfg is None:
        raise ToolError("这个租户没有开采购仓储模块")
    return cfg, ctx.extras.get("orders") or OrdersConfig(), ctx.extras.get("tz") or ZoneInfo("Asia/Shanghai")


async def _material(ctx: ToolContext, key: str) -> str:
    hits = await find_by(ctx.session, ctx.tenant_id, "material", "name", key)
    if len(hits) != 1:
        raise ToolError(f"物料 {key} 没找到或对应多种物料")
    return hits[0].object_id


class StockArgs(BaseModel):
    material: str | None = Field(default=None, max_length=64, description="物料 id 或名称；不填 = 全部物料")


@tool("stock.query", "实时库存：每种物料的可用、待检、不可用数量、安全库存和批次（先进先出顺序）", StockArgs)
async def stock_query(ctx: ToolContext, a: StockArgs) -> dict[str, Any]:
    ctx.require("warehouse.material_lot:read")
    cfg, ocfg, _ = _env(ctx)
    ids = {await _material(ctx, a.material)} if a.material else None
    return await material_stock(ctx.session, ctx.tenant_id, cfg, ocfg, material_ids=ids)


class NoArgs(BaseModel):
    pass


@tool(
    "purchase.suggestions",
    "请购建议：按当前排窑计划的原料缺口和安全库存，扣掉还没下单的请购后，每种物料建议请购多少、哪天前要、建议供应商",
    NoArgs,
)
async def purchase_suggestions(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    ctx.require("purchase.purchase_requisition:read")
    cfg, ocfg, tz = _env(ctx)
    return await suggestions(
        ctx.session, ctx.tenant_id, cfg, ocfg, planning=ctx.extras.get("planning"), tz=tz, now=utcnow()
    )


class CompareArgs(BaseModel):
    material: str = Field(max_length=64, description="物料 id 或名称")
    qty: float | None = Field(default=None, gt=0, description="采购数量（算金额、看起订量）")
    need_date: date | None = Field(default=None, description="需要到货的日期 YYYY-MM-DD")


@tool(
    "purchase.compare",
    "比价：一种物料的有效供应商报价（单价、金额、交货周期、能否按需要日期到、供应商评分）和推荐",
    CompareArgs,
)
async def purchase_compare(ctx: ToolContext, a: CompareArgs) -> dict[str, Any]:
    ctx.require("purchase.supplier_quote:read")
    cfg, ocfg, tz = _env(ctx)
    try:
        return await compare_quotes(
            ctx.session,
            ctx.tenant_id,
            cfg,
            ocfg,
            material_id=await _material(ctx, a.material),
            qty=a.qty,
            need_date=a.need_date,
            today=utcnow().astimezone(tz).date(),
            tz=tz,
        )
    except SupplyError as e:
        raise ToolError(str(e)) from e


@tool("purchase.supplier_scores", "供应商评分：最近一段时间的到货批次数、来料合格率、准时率和总分", NoArgs)
async def supplier_scores(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    ctx.require("purchase.supplier:read")
    cfg, ocfg, tz = _env(ctx)
    rows = await score_suppliers(
        ctx.session, ctx.tenant_id, cfg, ocfg, today=utcnow().astimezone(tz).date(), tz=tz
    )
    return {"suppliers": rows, "window_days": cfg.suppliers.window_days, "formula": FORMULA}
