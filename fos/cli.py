"""管理命令。

fos tenants                         列出租户配置并校验
fos create-user <tenant> <login> <显示名> --roles admin,sales
fos issue-key <tenant> <login> [--label 说明]
fos revoke-key <key_id>
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from fos.config import get_settings
from fos.db.models import ApiKey, User, utcnow
from fos.db.session import make_engine, make_sessionmaker
from fos.governance import apikeys
from fos.ledger.projection import rebuild
from fos.ontology.loader import load_ontology
from fos.tenancy import TenantRegistry


async def _run(args: argparse.Namespace) -> int:
    s = get_settings()
    registry = TenantRegistry.load(s.tenants_dir)
    if args.cmd == "tenants":
        for tid in registry.ids():
            t = registry.get(tid)
            assert t
            print(f"{t.id}\t{t.name}\tpack={t.industry_pack}\tim={','.join(t.im)}\troles={len(t.roles)}")
        return 0
    if args.cmd in ("ontology", "rebuild"):
        tenant = registry.get(args.tenant)
        if not tenant:
            print(f"租户 {args.tenant} 不存在", file=sys.stderr)
            return 1
        onto = load_ontology(tenant, packs_dir=s.packs_dir, tenants_dir=s.tenants_dir)
        if args.cmd == "ontology":
            print("层：" + " → ".join(onto.layers))
            for k, o in onto.objects.items():
                print(f"  对象 {k:<18} {o.name}（{len(o.properties)} 个属性，域 {o.domain}）")
            for k, e in onto.events.items():
                print(f"  事件 {k:<36} {e.name}  [{e.op} {e.object}，需 {e.permission}]")
            return 0

    engine = make_engine(s.database_url)
    sm = make_sessionmaker(engine)
    try:
        async with sm() as session, session.begin():
            if args.cmd == "create-user":
                tenant = registry.get(args.tenant)
                if not tenant:
                    print(f"租户 {args.tenant} 不存在", file=sys.stderr)
                    return 1
                roles = [r for r in args.roles.split(",") if r]
                unknown = [r for r in roles if r not in tenant.roles]
                if unknown:
                    print(f"未定义的角色：{unknown}", file=sys.stderr)
                    return 1
                u = User(tenant_id=tenant.id, login=args.login, display_name=args.display_name, roles=roles)
                session.add(u)
                await session.flush()
                print(u.id)
            elif args.cmd == "issue-key":
                u = (
                    await session.execute(
                        select(User).where(User.tenant_id == args.tenant, User.login == args.login)
                    )
                ).scalar_one_or_none()
                if not u:
                    print("用户不存在", file=sys.stderr)
                    return 1
                k = await apikeys.issue(session, u, label=args.label)
                print(k.plaintext)
                print("（只显示这一次，请妥善保存）", file=sys.stderr)
            elif args.cmd == "rebuild":
                n = await rebuild(session, onto, tenant.id)
                print(f"已重放 {n} 个事件")
            elif args.cmd == "revoke-key":
                k = await session.get(ApiKey, args.key_id)
                if not k:
                    print("key 不存在", file=sys.stderr)
                    return 1
                k.revoked_at = utcnow()
                print("已吊销")
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fos")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tenants")
    cu = sub.add_parser("create-user")
    cu.add_argument("tenant")
    cu.add_argument("login")
    cu.add_argument("display_name")
    cu.add_argument("--roles", default="")
    ik = sub.add_parser("issue-key")
    ik.add_argument("tenant")
    ik.add_argument("login")
    ik.add_argument("--label", default="")
    rk = sub.add_parser("revoke-key")
    rk.add_argument("key_id")
    for name in ("ontology", "rebuild"):
        sp = sub.add_parser(name)
        sp.add_argument("tenant")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
