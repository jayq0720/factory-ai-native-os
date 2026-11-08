"""工程演示：把演示知识库（packs/<pack>/engineering/sample_kb.yaml）写入账本。

演示知识库的牌号、热工参数、价格都是示例值，以模拟数据（data_mode=mock）写入：
用它算出来的方案只能演示，不能发布报价、不能导出正式文件。真实知识库由知识库管理员用 kb.* 事件维护。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

import yaml
from sqlalchemy import select

from fos.config import Settings
from fos.db.models import ObjectState, User
from fos.db.session import make_engine, make_sessionmaker
from fos.governance.principal import principal_for_user
from fos.ledger import AppendRequest, Ledger
from fos.ontology.loader import load_ontology
from fos.tenancy import TenantRegistry


def kb_requests(path: Path, data_mode: Literal["real", "mock"] = "mock") -> list[AppendRequest]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = []
    for p in data["products"]:
        out.append(
            AppendRequest(
                type="kb.product_registered",
                object_id=f"P-{p['code']}",
                payload=p,
                data_mode=data_mode,
                idempotency_key=f"kb-demo:P-{p['code']}",
            )
        )
    for pr in data["prices"]:
        code = pr.pop("code")
        out.append(
            AppendRequest(
                type="kb.price_set",
                object_id=f"PR-{code}",
                payload={"product": f"P-{code}", **pr},
                data_mode=data_mode,
                idempotency_key=f"kb-demo:PR-{code}",
            )
        )
    return out


async def seed_kb(s: Settings, tenant_id: str, login: str) -> int:
    """以模拟数据写入演示知识库（可重复执行：已有的对象跳过）。"""
    registry = TenantRegistry.load(s.tenants_dir)
    tenant = registry.get(tenant_id)
    if tenant is None:
        print(f"租户 {tenant_id} 不存在", file=sys.stderr)
        return 1
    path = s.packs_dir / tenant.industry_pack / "engineering" / "sample_kb.yaml"
    if not path.exists():
        print(f"行业包 {tenant.industry_pack} 没有演示知识库", file=sys.stderr)
        return 1
    onto = load_ontology(tenant, packs_dir=s.packs_dir, tenants_dir=s.tenants_dir)
    ledger = Ledger(lambda _t: onto)
    engine = make_engine(s.database_url)
    n = 0
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            user = (
                await session.scalars(select(User).where(User.tenant_id == tenant_id, User.login == login))
            ).one_or_none()
            if user is None:
                print(f"用户 {login} 不存在（先执行 fos quickstart）", file=sys.stderr)
                return 1
            principal = principal_for_user(user, tenant)
            reqs = kb_requests(path)
            creds = s.packs_dir / tenant.industry_pack / "tender" / "sample_credentials.yaml"
            if creds.exists() and "credential" in onto.objects:  # 招标拆解的演示资质资料库
                from datetime import date

                from fos.tender.samples import credential_requests

                reqs += credential_requests(creds, date.today())
            for req in reqs:
                obj = onto.events[req.type].object
                if await session.get(ObjectState, (tenant_id, obj, req.object_id)) is not None:
                    continue
                await ledger.append(session, principal, req)
                n += 1
    finally:
        await engine.dispose()
    print(f"已写入演示知识库与资质资料 {n} 条（模拟数据）。")
    return 0
