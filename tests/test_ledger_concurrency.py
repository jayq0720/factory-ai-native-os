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
