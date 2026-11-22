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
    from fos.channels.base import ChannelAdapter
    from fos.channels.dingtalk import DingTalkAdapter, DingTalkApp, DingTalkStream
    from fos.channels.feishu import FeishuAdapter, FeishuApp
    from fos.channels.types import InboundMessage
    from fos.channels.wecom import WeComAdapter, WeComApp
    from fos.monitoring.watch import monitor_loop
    from fos.planning.notify import order_watch_loop
    from fos.reports.schedule import daily_loop
    from fos.services import Services
    from fos.tender.notify import tender_watch_loop
    from fos.triggers.intake import deliver

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    svc = Services.build(get_settings())
    runs = []
    adapters: list[ChannelAdapter] = []
    for tid in tenant_ids or svc.registry.ids():
        tenant = svc.registry.get(tid)
        if tenant is None:
            continue
        tenant_adapters: list[ChannelAdapter] = []
        if "dingtalk" in tenant.im:
            app = DingTalkApp.from_env(tid)
            if app is None:
                print(
                    f"{tid}: 没有配置 FOS_DINGTALK_{tid.upper()}_CLIENT_ID / _CLIENT_SECRET，跳过钉钉",
                    file=sys.stderr,
                )
            else:
                dt_adapter = DingTalkAdapter(app)
                adapters.append(dt_adapter)
                tenant_adapters.append(dt_adapter)

                async def handle(msg: InboundMessage, adapter: DingTalkAdapter = dt_adapter) -> None:
                    async with svc.sessionmaker() as session, session.begin():
                        out = await svc.intake.handle(session, adapter, msg)
                    await deliver(adapter, out)  # 提交之后再回复
                    t = out.trigger
                    if t is not None and not out.duplicate and t.route == "router" and t.status == "pending":
                        await svc.runtime.process(svc.sessionmaker, adapter, t.id)

                runs.append(DingTalkStream(app, handle).run_forever())
                print(f"{tid}: 钉钉 Stream 启动")
        if "feishu" in tenant.im:
            fs_app = FeishuApp.from_env(tid)
            if fs_app is not None:
                fs_adapter = FeishuAdapter(fs_app)
                adapters.append(fs_adapter)
                tenant_adapters.append(fs_adapter)
                print(f"{tid}: 飞书适配器就绪（回调路径 /v1/channels/feishu/{tid}/webhook）")
        if "wecom" in tenant.im:
            wc_app = WeComApp.from_env(tid)
            if wc_app is not None:
                wc_adapter = WeComAdapter(wc_app)
                adapters.append(wc_adapter)
                tenant_adapters.append(wc_adapter)
                print(f"{tid}: 企业微信适配器就绪（回调路径 /v1/channels/wecom/{tid}/webhook）")

        for adapter in tenant_adapters:
            sched = tenant.reports.daily
            if sched is not None and sched.to:
                runs.append(daily_loop(svc, tid, adapter))
                print(f"{tid}: [{adapter.name}] 每天 {sched.at} 推送生产日报给 {'、'.join(sched.to)}")
            if tid in svc.planning:
                runs.append(order_watch_loop(svc, tid, adapter))
                roles = "、".join(svc.planning[tid].watch.notify_roles)
                print(f"{tid}: [{adapter.name}] 新订单自动评估交期，推给 {roles}")
            if tid in svc.tenders:
                runs.append(tender_watch_loop(svc, tid, adapter))
                hours = "/".join(f"{h:g}" for h in svc.tenders[tid].reminders.offsets_hours)
                roles = "、".join(svc.tenders[tid].reminders.notify_roles)
                print(f"{tid}: [{adapter.name}] 投标截止前 {hours} 小时提醒负责人和 {roles}")
            if tid in svc.monitoring:
                runs.append(monitor_loop(svc, tid, adapter))
                m = svc.monitoring[tid]
                roles = "、".join(m.notify.alarm_roles)
                print(f"{tid}: [{adapter.name}] 窑炉监控每 {m.alarms.poll_seconds:g}s 判定，推给 {roles}")
    if not runs:
        print("没有可启动的信道", file=sys.stderr)
        await svc.aclose()
        return 1
    try:
        await asyncio.gather(*runs)
    finally:
        for ad in adapters:
            await ad.aclose()
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
    if args.cmd == "clone-tenant":
        from fos.ops.provision import provision_tenant

        im_list = [x.strip() for x in args.im.split(",") if x.strip()]
        try:
            rep = provision_tenant(
                packs_dir=s.packs_dir,
                tenants_dir=s.tenants_dir,
                tenant_id=args.tenant_id,
                name=args.name,
                industry_pack=args.pack,
                im=im_list,
                template_tenant=args.template or None,
            )
        except ValueError as e:
            print(f"创建失败：{e}", file=sys.stderr)
            return 1
        print(
            f"已创建并验证新租户 {rep['tenant_id']}（{rep['name']}）-> {rep['tenant_dir']} | "
            f"对象 {rep['objects_count']} / 事件 {rep['events_count']} / "
            f"规则 {rep['policy_rules_count']} / Skill {rep['skills_count']}"
        )
        return 0
    if args.cmd == "monthly-report":
        from datetime import date
        from pathlib import Path

        from fos.reports.monthly import build_monthly_report, render_monthly_xlsx
        from fos.services import Services

        svc = Services.build(s)
        try:
            month = args.month or date.today().strftime("%Y-%m")
            async with svc.sessionmaker() as session:
                try:
                    rep = await build_monthly_report(session, svc, args.tenant, month)
                except ValueError as e:
                    print(f"生成失败：{e}", file=sys.stderr)
                    return 1
            print(rep["markdown"])
            if args.xlsx:
                out_path = Path(args.xlsx)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(render_monthly_xlsx(rep))
                print(f"已导出 Excel：{out_path}")
            return 0
        finally:
            await svc.aclose()
    if args.cmd == "backup-ledger":
        from pathlib import Path

        from fos.ops.backup import export_tenant_ledger_jsonl
        from fos.services import Services

        svc = Services.build(s)
        try:
            if svc.registry.get(args.tenant) is None:
                print(f"租户 {args.tenant} 不存在", file=sys.stderr)
                return 1
            mode = None if args.include_mock else "real"
            async with svc.sessionmaker() as session:
                content, summary = await export_tenant_ledger_jsonl(session, args.tenant, data_mode=mode)
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            print(
                f"已导出 {summary['tenant_id']} 账本灾备 -> {out_path} | "
                f"事件 {summary['count']} 条 | sha256={summary['sha256']}"
            )
            return 0
        finally:
            await svc.aclose()
    if args.cmd == "verify-backup":
        from pathlib import Path

        from fos.ops.backup import verify_ledger_backup_jsonl

        p_file = Path(args.file)
        if not p_file.exists():
            print(f"文件不存在：{p_file}", file=sys.stderr)
            return 1
        res = verify_ledger_backup_jsonl(p_file.read_text(encoding="utf-8"))
        if not res["ok"]:
            print(f"校验失败：{res['error']}", file=sys.stderr)
            return 1
        print(
            f"校验通过：{res['tenant_id']} | 事件 {res['count']} 条 | "
            f"seq={res['first_seq']}..{res['last_seq']} | sha256={res['sha256']}"
        )
        return 0
    if args.cmd == "restore-ledger":
        from pathlib import Path

        from fos.ops.backup import restore_tenant_ledger_jsonl
        from fos.services import Services

        p_file = Path(args.file)
        if not p_file.exists():
            print(f"文件不存在：{p_file}", file=sys.stderr)
            return 1
        svc = Services.build(s)
        try:
            onto = svc.ontologies.get(args.tenant)
            if onto is None:
                print(f"租户 {args.tenant} 不存在", file=sys.stderr)
                return 1
            raw = p_file.read_text(encoding="utf-8")
            try:
                async with svc.sessionmaker() as session, session.begin():
                    res = await restore_tenant_ledger_jsonl(session, onto, args.tenant, raw, force=args.force)
            except ValueError as e:
                print(f"恢复失败：{e}", file=sys.stderr)
                return 1
            print(
                f"恢复完成：{res['tenant_id']} | 恢复事件 {res['restored_events']} 条 | "
                f"重放投影 {res['replayed_events']} 条 | last_seq={res['last_seq']}"
            )
            return 0
        finally:
            await svc.aclose()
    if args.cmd in ("eval", "security-check", "watchdog"):
        from datetime import date
        from pathlib import Path

        from fos.eval.runner import run_golden_eval
        from fos.ops.service import security_baseline
        from fos.ops.watchdog import run_tenant_watchdog, watchdog_loop
        from fos.services import Services

        svc = Services.build(s)
        try:
            if args.cmd == "watchdog":
                if args.all:
                    targets = list(svc.registry.ids())
                else:
                    if not args.tenant or svc.registry.get(args.tenant) is None:
                        print(f"租户 {args.tenant or '(未指定)'} 不存在", file=sys.stderr)
                        return 1
                    targets = [args.tenant]
                if args.interval and args.interval > 0:
                    logging.basicConfig(
                        level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    )
                    await watchdog_loop(svc, targets, interval_seconds=float(args.interval))
                    return 0
                today = date.today()
                for tid in targets:
                    async with svc.sessionmaker() as session, session.begin():
                        res = await run_tenant_watchdog(session, svc, tid, today)
                    print(
                        f"{res['tenant_id']} Watchdog ({res['date']}): "
                        f"新建待办 {res['total_todos_created']} 条，"
                        f"清理超时审批 {res['expired_approvals']} 条"
                    )
                    for mod, cnt in res["modules"].items():
                        print(f"  {mod:<24} +{cnt}")
                return 0
            if svc.registry.get(args.tenant) is None:
                print(f"租户 {args.tenant} 不存在", file=sys.stderr)
                return 1
            if args.cmd == "eval":
                sp = Path(args.suite) if args.suite else None
                res = run_golden_eval(svc, args.tenant, suite_path=sp)
                print(
                    f"{res['tenant_id']} [{res['suite']}]: "
                    f"{res['passed']}/{res['total']} ({res['pass_rate_pct']}%)"
                )
                for cat, st in res["categories"].items():
                    print(f"  {cat:<28} {st['passed']}/{st['total']} (failed={st['failed']})")
                for bc in res["badcases"]:
                    print(
                        f"  [BADCASE] {bc['category']}:{bc['id']} "
                        f"expect={bc['expected']!r} got={bc['actual']!r}"
                    )
                return 0 if res["all_passed"] else 1
            res = security_baseline(svc, args.tenant)
            for chk in res["checks"]:
                mark = "OK" if chk["ok"] else "FAIL"
                print(f"  [{mark:<4}] ({chk['severity']:<5}) {chk['name']}: {chk['detail']}")
            return 0 if res["passed"] else 1
        finally:
            await svc.aclose()
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
                k = await apikeys.issue(session, u, label=args.label, edge_id=args.edge_id or None)
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
    ik.add_argument("--edge-id", dest="edge_id", default="", help="绑定厂端网关 edge_id（仅限该网关上行）")
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
    ev = sub.add_parser("eval", help="运行行业包黄金数据集回归评测")
    ev.add_argument("tenant")
    ev.add_argument("--suite", default="", help="自定义黄金评测 YAML 路径")
    sc = sub.add_parser("security-check", help="运行租户安全与上线基线自检")
    sc.add_argument("tenant")
    wd = sub.add_parser("watchdog", help="执行全厂统一 Watchdog 巡检（支持单次或守护循环）")
    wd.add_argument("tenant", nargs="?", default="")
    wd.add_argument("--all", action="store_true", help="巡检全部已注册租户")
    wd.add_argument("--interval", type=int, default=0, help="周期守护间隔（秒），>0 时常驻循环")
    ct = sub.add_parser("clone-tenant", help="一键复制新工厂租户目录并校验就绪")
    ct.add_argument("tenant_id")
    ct.add_argument("name")
    ct.add_argument("--pack", default="refractory", help="行业包名称")
    ct.add_argument("--im", default="dingtalk", help="启用的 IM 信道（逗号分隔）")
    ct.add_argument("--template", default="luneng", help="模板租户（留空则仅生成最小角色）")
    mr = sub.add_parser("monthly-report", help="生成全厂月度经营与合规汇总报表（Markdown + Excel）")
    mr.add_argument("tenant")
    mr.add_argument("--month", default="", help="统计月份 YYYY-MM（默认当月）")
    mr.add_argument("--xlsx", default="", help="可选：导出正式三表 Excel 文件路径")
    bl = sub.add_parser("backup-ledger", help="导出租户不可变事件账本防篡改灾备文件（JSONL + SHA-256）")
    bl.add_argument("tenant")
    bl.add_argument("out")
    bl.add_argument("--include-mock", action="store_true", help="包含模拟数据（默认仅真实账本）")
    vb = sub.add_parser("verify-backup", help="离线校验账本灾备 JSONL 完整性与 SHA-256")
    vb.add_argument("file")
    rl = sub.add_parser("restore-ledger", help="校验防篡改灾备文件并恢复租户账本与投影")
    rl.add_argument("tenant")
    rl.add_argument("file")
    rl.add_argument("--force", action="store_true", help="目标租户已有事件时允许覆盖恢复")
    sub.add_parser("edge-keygen", help="生成 fos-edge 命令签名密钥对（私钥进环境变量，公钥进 edge.toml）")
    args = p.parse_args(argv)
    if args.cmd == "edge-keygen":
        from fos.monitoring.edge import CommandSigner

        seed, pub = CommandSigner.generate()
        print("# 私钥：只放进云端环境变量 / KMS（凭据管家），不要写进代码、租户配置或 edge.toml")
        print(f"FOS_EDGE_COMMAND_KEY={seed}")
        print("# 公钥：填进每台网关 edge.toml")
        print(f'command_public_key = "{pub}"')
        return 0
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
