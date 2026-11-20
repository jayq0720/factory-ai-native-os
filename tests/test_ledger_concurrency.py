"""并发追加：同一租户的 seq 不重复、不跳号。只在 PostgreSQL 上有意义。"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select

from fos.db.models import Event
from fos.db.session import make_engine, make_sessionmaker
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, Ledger
from fos.ontology.loader import load_ontology
from fos.tenancy import TenantRegistry

pytestmark = pytest.mark.skipif(
    not (os.environ.get("FOS_TEST_DATABASE_URL") or "").startswith("postgresql"),
    reason="需要 FOS_TEST_DATABASE_URL 指向 PostgreSQL",
)


def test_concurrent_appends_get_gapless_seq(settings, keys, tenants_dir):
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    tenant = TenantRegistry.load(tenants_dir).get("luneng")
    assert tenant
    onto = load_ontology(tenant, packs_dir=repo / "packs", tenants_dir=tenants_dir)
    ledger = Ledger(lambda _: onto)
    who = Principal("luneng", "u1", "user", "测试", ("admin",), frozenset({"*.*:*"}))

    async def run() -> list[int]:
        engine = make_engine(settings.database_url)
        sm = make_sessionmaker(engine)

        async def one(i: int) -> None:
            async with sm() as s, s.begin():
                req = AppendRequest(
                    type="sales.customer_registered", object_id=f"C{i}", payload={"name": f"客户{i}"}
                )
                await ledger.append(s, who, req)

        try:
            await asyncio.gather(*(one(i) for i in range(30)))
            async with sm() as s:
                return list(
                    (
                        await s.scalars(
                            select(Event.seq).where(Event.tenant_id == "luneng").order_by(Event.seq)
                        )
                    ).all()
                )
        finally:
            await engine.dispose()

    assert asyncio.run(run()) == list(range(1, 31))


def test_concurrent_stock_issue_prevents_negative_balance(settings, keys, tenants_dir):
    """两条并发领料（各 8 吨，批次共 10 吨）在 PostgreSQL 下必须串行校验，只允许成功一条。"""
    from pathlib import Path

    from fos.ledger import Conflict, Invalid
    from fos.orders.config import load_orders_config
    from fos.supply.config import load_supply_config
    from fos.supply.guards import ISSUE_EVENT, SupplyGuard
    from fos.supply.stock import lot_balance

    repo = Path(__file__).resolve().parents[1]
    tenant = TenantRegistry.load(tenants_dir).get("luneng")
    assert tenant
    onto = load_ontology(tenant, packs_dir=repo / "packs", tenants_dir=tenants_dir)
    scfg = load_supply_config(tenant, packs_dir=repo / "packs", tenants_dir=tenants_dir)
    ocfg = load_orders_config(tenant, packs_dir=repo / "packs", tenants_dir=tenants_dir)
    ledger = Ledger(lambda _: onto)
    ledger.add_guard(ISSUE_EVENT, SupplyGuard(ISSUE_EVENT, {"luneng": scfg}, {"luneng": ocfg}))
    who = Principal("luneng", "u1", "user", "仓管", ("admin",), frozenset({"*.*:*"}))

    async def run() -> tuple[int, int, float]:
        engine = make_engine(settings.database_url)
        sm = make_sessionmaker(engine)
        try:
            async with sm() as s, s.begin():
                await ledger.append(
                    s,
                    who,
                    AppendRequest(
                        type="warehouse.material_defined",
                        object_id="M-CONC",
                        payload={"name": "莫来石", "category": "raw", "unit": "吨"},
                    ),
                )
                await ledger.append(
                    s,
                    who,
                    AppendRequest(
                        type="warehouse.material_received",
                        object_id="LOT-CONC",
                        payload={
                            "material": "M-CONC",
                            "qty": 10,
                            "unit": "吨",
                            "status": "accepted",
                        },
                    ),
                )

            async def issue_one(i: int) -> bool:
                try:
                    async with sm() as s, s.begin():
                        await ledger.append(
                            s,
                            who,
                            AppendRequest(
                                type="warehouse.stock_issued",
                                object_id=f"MV-CONC-{i}",
                                payload={
                                    "lot": "LOT-CONC",
                                    "material": "M-CONC",
                                    "kind": "issue",
                                    "qty": 8,
                                    "unit": "吨",
                                },
                            ),
                        )
                    return True
                except (Conflict, Invalid):
                    return False

            results = await asyncio.gather(*(issue_one(i) for i in range(4)))
            async with sm() as s:
                bal = await lot_balance(s, "luneng", "LOT-CONC")
                assert bal is not None
                return results.count(True), results.count(False), bal.on_hand
        finally:
            await engine.dispose()

    ok_cnt, fail_cnt, on_hand = asyncio.run(run())
    assert ok_cnt == 1 and fail_cnt == 3 and on_hand == pytest.approx(2.0)
