"""工程知识库读取：产品（含热工参数）与价格都是账本对象的投影，按租户读取。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectState


def split_list(s: str | None) -> list[str]:
    if not s:
        return []
    for sep in ("，", "、", ";", "；", "/"):
        s = s.replace(sep, ",")
    return [x.strip() for x in s.split(",") if x.strip()]


@dataclass
class Price:
    id: str
    version: int
    price: float
    unit: str
    cost: float | None
    effective_from: str
    valid_until: str | None
    data_mode: str

    def expired(self, today: date) -> bool:
        return bool(self.valid_until) and date.fromisoformat(self.valid_until) < today

    def ref(self) -> str:
        return f"price_item/{self.id}@v{self.version}"


@dataclass
class Product:
    id: str
    version: int
    code: str
    name: str
    spec: str | None
    unit: str
    category: str | None
    classification_temp: float | None
    density: float | None  # g/cm³
    tc_a: float | None
    tc_b: float
    piece_volume: float | None
    loss_rate: float  # %
    unsuitable: list[str]
    aliases: list[str]
    data_mode: str
    price: Price | None = None

    @property
    def thermal_ready(self) -> bool:
        return self.tc_a is not None and self.classification_temp is not None and self.density is not None

    def label(self) -> str:
        return f"{self.code} {self.name}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "code": self.code,
            "name": self.name,
            "spec": self.spec,
            "unit": self.unit,
            "category": self.category,
            "classification_temp": self.classification_temp,
            "bulk_density": self.density,
            "tc_a": self.tc_a,
            "tc_b": self.tc_b,
            "piece_volume": self.piece_volume,
            "loss_rate": self.loss_rate,
            "unsuitable_atmospheres": self.unsuitable,
            "aliases": self.aliases,
            "data_mode": self.data_mode,
            "thermal_ready": self.thermal_ready,
            "price": None
            if self.price is None
            else {
                "id": self.price.id,
                "version": self.price.version,
                "price": self.price.price,
                "unit": self.price.unit,
                "cost": self.price.cost,
                "effective_from": self.price.effective_from,
                "valid_until": self.price.valid_until,
            },
        }


async def load_kb(
    session: AsyncSession, tenant_id: str, *, customer_class: str | None = None
) -> dict[str, Product]:
    """读取租户的产品与价格。同一产品多条价格时：优先匹配客户类别，其次取生效日期最新的。"""
    rows = (
        await session.execute(
            select(ObjectState).where(
                ObjectState.tenant_id == tenant_id, ObjectState.object_type.in_(["product", "price_item"])
            )
        )
    ).scalars()
    products: dict[str, Product] = {}
    prices: list[tuple[str, Price, str | None]] = []
    for o in rows:
        s = o.state
        if o.object_type == "product":
            products[o.object_id] = Product(
                id=o.object_id,
                version=o.version,
                code=s.get("code", o.object_id),
                name=s.get("name", ""),
                spec=s.get("spec"),
                unit=s.get("unit", "t"),
                category=s.get("category"),
                classification_temp=s.get("classification_temp"),
                density=s.get("bulk_density"),
                tc_a=s.get("tc_a"),
                tc_b=s.get("tc_b") or 0.0,
                piece_volume=s.get("piece_volume"),
                loss_rate=s.get("loss_rate") or 0.0,
                unsuitable=split_list(s.get("unsuitable_atmospheres")),
                aliases=split_list(s.get("aliases")),
                data_mode=o.data_mode,
            )
        else:
            prices.append(
                (
                    s["product"],
                    Price(
                        id=o.object_id,
                        version=o.version,
                        price=s["price"],
                        unit=s["unit"],
                        cost=s.get("cost"),
                        effective_from=s["effective_from"],
                        valid_until=s.get("valid_until"),
                        data_mode=o.data_mode,
                    ),
                    s.get("customer_class"),
                )
            )

    # 只用通用价格和该客户类别的价格；客户类别价优先，同级取生效日期最新的
    usable = [x for x in prices if not x[2] or x[2] == customer_class]
    for pid, price, _cls in sorted(usable, key=lambda x: (bool(x[2]), x[1].effective_from)):
        if pid in products:
            products[pid].price = price  # 排序靠后的覆盖靠前的
    return products
