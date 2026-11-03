"""管理命令。

fos tenants                         列出租户配置并校验
fos create-user <tenant> <login> <显示名> --roles admin,sales
fos issue-key <tenant> <login> [--label 说明]
fos revoke-key <key_id>
fos ontology <tenant> | rebuild <tenant>
fos bind-im <tenant> <login> <channel> <IM 账号 id>   绑定 IM 账号（钉钉为企业内 userid）
fos channels run [--tenant luneng]                   启动 IM 长连接（钉钉 Stream），收消息进 Trigger 入口
fos init-db                                          建表（SQLite 直接建表；PostgreSQL 执行迁移）
fos quickstart [--tenant luneng] [--login admin]     建表 + 建管理员 + 发 API Key（首次试用）
fos serve [--host 0.0.0.0] [--port 8000]             启动网页与接口，网页在 /app/
fos samples <目录>                                   生成样例文件（技术要求 / 招标文件 / 工程量清单）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select

from fos.config import get_settings
from fos.db.models import ApiKey, ChannelIdentity, User, utcnow
from fos.db.session import make_engine, make_sessionmaker
from fos.governance import apikeys
from fos.ledger.projection import rebuild
from fos.ontology.loader import load_ontology
from fos.tenancy import TenantRegistry


async def _run_channels(tenant_ids: list[str]) -> int:
    from fos.channels.dingtalk import DingTalkAdapter, DingTalkApp, DingTalkStream
    from fos.channels.types import InboundMessage
    from fos.services import Services
    from fos.triggers.intake import deliver

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    svc = Services.build(get_settings())
    runs = []
    for tid in tenant_ids or svc.registry.ids():
        tenant = svc.registry.get(tid)
        if tenant is None or "dingtalk" not in tenant.im:
            continue
        app = DingTalkApp.from_env(tid)
        if app is None:
            print(
                f"{tid}: 没有配置 FOS_DINGTALK_{tid.upper()}_CLIENT_ID / _CLIENT_SECRET，跳过",
                file=sys.stderr,
            )
            continue
        adapter = DingTalkAdapter(app)

        async def handle(msg: InboundMessage, adapter: DingTalkAdapter = adapter) -> None:
            async with svc.sessionmaker() as session, session.begin():
                out = await svc.intake.handle(session, adapter, msg)
            await deliver(adapter, out)  # 提交之后再回复
            t = out.trigger
            if t is not None and not out.duplicate and t.route == "router" and t.status == "pending":
                await svc.runtime.process(svc.sessionmaker, adapter, t.id)

        runs.append(DingTalkStream(app, handle).run_forever())
        print(f"{tid}: 钉钉 Stream 启动")
    if not runs:
        print("没有可启动的信道", file=sys.stderr)
        await svc.aclose()
        return 1
    try:
        await asyncio.gather(*runs)
    finally:
        await svc.aclose()
    return 0


async def _run(args: argparse.Namespace) -> int:
    s = get_settings()
    if args.cmd == "init-db":
        from fos.quickstart import init_db

        print(await init_db(s))
        return 0
    if args.cmd == "quickstart":
        from fos.quickstart import quickstart

        return await quickstart(s, args.tenant, args.login, args.display_name)
    if args.cmd == "channels":
        return await _run_channels([args.tenant] if args.tenant else [])
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
            elif args.cmd == "bind-im":
                u = (
                    await session.execute(
                        select(User).where(User.tenant_id == args.tenant, User.login == args.login)
                    )
                ).scalar_one_or_none()
                if not u:
                    print("用户不存在", file=sys.stderr)
                    return 1
                key = (args.tenant, args.channel, args.external_id)
                ident = await session.get(ChannelIdentity, key)
                if ident is None:
                    session.add(
                        ChannelIdentity(tenant_id=key[0], channel=key[1], external_id=key[2], user_id=u.id)
                    )
                else:
                    ident.user_id = u.id
                print(f"已绑定 {args.channel}:{args.external_id} → {u.login}")
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
    bi = sub.add_parser("bind-im")
    bi.add_argument("tenant")
    bi.add_argument("login")
    bi.add_argument("channel", choices=["dingtalk", "feishu", "wecom"])
    bi.add_argument("external_id")
    ch = sub.add_parser("channels")
    ch.add_argument("action", choices=["run"])
    ch.add_argument("--tenant", default="")
    for name in ("ontology", "rebuild"):
        sp = sub.add_parser(name)
        sp.add_argument("tenant")
    sub.add_parser("init-db")
    qs = sub.add_parser("quickstart")
    qs.add_argument("--tenant", default="luneng")
    qs.add_argument("--login", default="admin")
    qs.add_argument("--display-name", dest="display_name", default="管理员")
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sm = sub.add_parser("samples")
    sm.add_argument("out")
    args = p.parse_args(argv)
    if args.cmd == "serve":
        import uvicorn

        print(f"网页：http://{args.host}:{args.port}/app/    接口文档：http://{args.host}:{args.port}/docs")
        uvicorn.run("fos.api.app:app", host=args.host, port=args.port)
        return 0
    if args.cmd == "samples":
        from pathlib import Path

        from fos.quickstart import write_samples

        for f in write_samples(Path(args.out)):
            print(f)
        return 0
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
