"""管理命令。

fos tenants                         列出租户配置并校验
fos create-user <tenant> <login> <显示名> --roles admin,sales
fos issue-key <tenant> <login> [--label 说明]
fos revoke-key <key_id>
fos ontology <tenant> | rebuild <tenant>
fos policy <tenant>                 校验并查看审批规则（行业包 + 租户）
fos bind-im <tenant> <login> <channel> <IM 账号 id>   绑定 IM 账号（钉钉为企业内 userid）
fos channels run [--tenant luneng]                   启动 IM 长连接（钉钉 Stream），收消息进 Trigger 入口
fos init-db                                          建表（SQLite 直接建表；PostgreSQL 执行迁移）
fos quickstart [--tenant luneng] [--login admin]     建表 + 建管理员 + 发 API Key（首次试用）
fos serve [--host 0.0.0.0] [--port 8000]             启动网页与接口，网页在 /app/
fos samples <目录>                                   生成样例文件（技术要求 / 招标文件 / 工程量清单）
fos production-demo [目录]                         拍照录单演示：登记模拟主数据 + 导出五张单据照片
fos eng-demo                                       工程演示：写入演示知识库（模拟数据）
fos ocr-check [--image 图片]                          对当前配置的 OCR 服务发一张图，看能否识别、耗时多少
fos pilot-check <tenant>                              上线检查：逐项列出试点上线前还差什么（只读）
fos import-template <tenant> <out.xlsx>               生成主数据导入模板（按本体）
fos import <tenant> <file.xlsx> [--login admin] [--commit]   主数据导入：默认只预览，--commit 才入账
fos daily-report <tenant> [--date 2026-11-07] [--login admin] [--send]   生产日报；--send 推送给收报人
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
from fos.policy import load_policy
from fos.tenancy import TenantRegistry


async def _run_channels(tenant_ids: list[str]) -> int:
    from fos.channels.dingtalk import DingTalkAdapter, DingTalkApp, DingTalkStream
    from fos.channels.types import InboundMessage
    from fos.monitoring.watch import monitor_loop
    from fos.planning.notify import order_watch_loop
    from fos.reports.schedule import daily_loop
    from fos.services import Services
    from fos.tender.notify import tender_watch_loop
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
        sched = tenant.reports.daily
        if sched is not None and sched.to:
            runs.append(daily_loop(svc, tid, adapter))
            print(f"{tid}: 每天 {sched.at} 推送生产日报给 {'、'.join(sched.to)}")
        if tid in svc.planning:
            runs.append(order_watch_loop(svc, tid, adapter))
            print(f"{tid}: 新订单自动评估交期，推给 {'、'.join(svc.planning[tid].watch.notify_roles)}")
        if tid in svc.tenders:
            runs.append(tender_watch_loop(svc, tid, adapter))
            hours = "/".join(f"{h:g}" for h in svc.tenders[tid].reminders.offsets_hours)
            roles = "、".join(svc.tenders[tid].reminders.notify_roles)
            print(f"{tid}: 投标截止前 {hours} 小时提醒负责人和 {roles}")
        if tid in svc.monitoring:
            runs.append(monitor_loop(svc, tid, adapter))
            m = svc.monitoring[tid]
            roles = "、".join(m.notify.alarm_roles)
            print(f"{tid}: 窑炉监控每 {m.alarms.poll_seconds:g} 秒判定一轮，告警推给 {roles}")
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

        return await quickstart(s, args.tenant, args.login, args.display_name, new_key=args.new_key)
    if args.cmd == "ocr-check":
        from pathlib import Path

        from fos.quickstart import ocr_check

        return await ocr_check(s, Path(args.image) if args.image else None)
    if args.cmd == "production-demo":
        from pathlib import Path

        from fos.entry.samples import seed_master, write_photos

        rc = await seed_master(s, args.tenant, args.login)
        if rc == 0:
            for f in write_photos(Path(args.out)):
                print(f"照片：{f}")
            print("在 IM（或模拟信道）依次发这五张照片，确认卡片后发“追溯 B261106-01”。")
        return rc
    if args.cmd == "eng-demo":
        from fos.engineering.samples import seed_kb

        rc = await seed_kb(s, args.tenant, args.login)
        if rc == 0:
            print("打开 /app/eng.html：上传 fos samples 生成的技术要求 → 核对字段 → 建工程项目 → 匹配方案。")
        return rc
    if args.cmd in ("pilot-check", "import-template", "import", "daily-report"):
        from pathlib import Path

        from fos.onboarding import commands

        if args.cmd == "pilot-check":
            return await commands.check(s, args.tenant)
        if args.cmd == "import-template":
            return await commands.import_template(s, args.tenant, Path(args.out))
        if args.cmd == "import":
            return await commands.import_file(s, args.tenant, args.login, Path(args.file), commit=args.commit)
        return await commands.daily(s, args.tenant, args.login, args.date, send=args.send)
    if args.cmd == "channels":
        return await _run_channels([args.tenant] if args.tenant else [])
    registry = TenantRegistry.load(s.tenants_dir)
    if args.cmd == "tenants":
        for tid in registry.ids():
            t = registry.get(tid)
            assert t
            print(f"{t.id}\t{t.name}\tpack={t.industry_pack}\tim={','.join(t.im)}\troles={len(t.roles)}")
        return 0
    if args.cmd == "policy":
        tenant = registry.get(args.tenant)
        if not tenant:
            print(f"租户 {args.tenant} 不存在", file=sys.stderr)
            return 1
        onto = load_ontology(tenant, packs_dir=s.packs_dir, tenants_dir=s.tenants_dir)
        pol = load_policy(tenant, packs_dir=s.packs_dir, tenants_dir=s.tenants_dir, ontology=onto)
        print("层：" + (" → ".join(pol.layers) or "（无规则，全部直接入账）"))
        for rid, r in pol.rules.items():
            when = " 且 ".join(c.describe() for c in r.when) or "总是"
            then = f"拒绝：{r.reason}" if r.effect == "deny" else " → ".join(st.name for st in r.steps)
            print(f"  {rid:<24} {r.name}\n      事件 {', '.join(r.events)}；条件 {when}；{then}")
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
    pd = sub.add_parser("production-demo", help="拍照录单演示：登记模拟主数据并导出五张单据照片")
    pd.add_argument("out", nargs="?", default="samples/production")
    pd.add_argument("--tenant", default="luneng")
    pd.add_argument("--login", default="admin")
    ed = sub.add_parser("eng-demo", help="工程演示：写入演示知识库（模拟数据）")
    ed.add_argument("--tenant", default="luneng")
    ed.add_argument("--login", default="admin")
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
    for name in ("ontology", "rebuild", "policy"):
        sp = sub.add_parser(name)
        sp.add_argument("tenant")
    sub.add_parser("init-db")
    qs = sub.add_parser("quickstart")
    qs.add_argument("--tenant", default="luneng")
    qs.add_argument("--login", default="admin")
    qs.add_argument("--display-name", dest="display_name", default="管理员")
    qs.add_argument("--new-key", action="store_true", help="用户已存在时也再发一把 Key")
    oc = sub.add_parser("ocr-check")
    oc.add_argument("--image", default="")
    pc = sub.add_parser("pilot-check", help="上线检查（只读）")
    pc.add_argument("tenant")
    it = sub.add_parser("import-template", help="生成主数据导入模板")
    it.add_argument("tenant")
    it.add_argument("out")
    im = sub.add_parser("import", help="主数据导入：默认只预览")
    im.add_argument("tenant")
    im.add_argument("file")
    im.add_argument("--login", default="admin")
    im.add_argument("--commit", action="store_true", help="没有错误时入账")
    dr = sub.add_parser("daily-report", help="生产日报")
    dr.add_argument("tenant")
    dr.add_argument("--date", default="")
    dr.add_argument("--login", default="admin", help="按谁的权限出报告（不推送时）")
    dr.add_argument("--send", action="store_true", help="推送给 tenant.yaml 里的收报人（同一天只推一次）")
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
