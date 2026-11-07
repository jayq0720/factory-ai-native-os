"""试点上线的命令行实现（fos import-template / import / daily-report / pilot-check）。"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from fos.config import Settings
from fos.db.models import User, utcnow


def _svc(s: Settings):
    from fos.services import Services

    return Services.build(s)


async def import_template(s: Settings, tenant_id: str, out: Path) -> int:
    from fos.onboarding.importer import build_template

    svc = _svc(s)
    try:
        tenant = svc.registry.get(tenant_id)
        if tenant is None:
            print(f"租户 {tenant_id} 不存在", file=sys.stderr)
            return 1
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(build_template(svc.imports[tenant_id], svc.ontologies[tenant_id], tenant.name))
        print(f"模板：{out}")
        return 0
    finally:
        await svc.aclose()


async def import_file(s: Settings, tenant_id: str, login: str, path: Path, *, commit: bool) -> int:
    from fos.governance.principal import principal_for_user
    from fos.onboarding.importer import ImportFileError, run_import

    svc = _svc(s)
    try:
        tenant = svc.registry.get(tenant_id)
        if tenant is None:
            print(f"租户 {tenant_id} 不存在", file=sys.stderr)
            return 1
        data = path.read_bytes()
        async with svc.sessionmaker() as session, session.begin():
            u = (
                await session.scalars(select(User).where(User.tenant_id == tenant_id, User.login == login))
            ).one_or_none()
            if u is None:
                print(f"用户 {login} 不存在", file=sys.stderr)
                return 1
            principal = principal_for_user(u, tenant)
            if not principal.can("platform.import:write"):
                print(f"{login} 没有导入权限（platform.import:write）", file=sys.stderr)
                return 1
            digest = svc.blobs.put(tenant_id, data) if commit else ""
            try:
                rep = await run_import(
                    session,
                    ledger=svc.ledger,
                    principal=principal,
                    onto=svc.ontologies[tenant_id],
                    cfg=svc.imports[tenant_id],
                    tz=ZoneInfo(tenant.timezone),
                    data=data,
                    filename=path.name,
                    evidence_ref=svc.blobs.uri(tenant_id, digest) if commit else "preview",
                    commit=commit,
                )
            except ImportFileError as e:
                print(str(e), file=sys.stderr)
                return 1
        for w in rep.warnings:
            print(f"! {w}")
        for sh in rep.sheets:
            n = {k: sh.count(k) for k in ("created", "exists", "error")}  # type: ignore[arg-type]
            print(f"{sh.name}：新增 {n['created']}，已存在 {n['exists']}，错误 {n['error']}")
            for e in sh.errors:
                print(f"  ✗ {e}")
            for r in sh.rows:
                if r.status == "error":
                    print(f"  ✗ 第 {r.row} 行 {r.label}：{r.message}")
        if rep.committed:
            print("已入账。")
        elif commit:
            print("有错误，一条也没有写入。改好后重新导入。", file=sys.stderr)
            return 1
        else:
            print("预览完成，没有写入。确认无误后加 --commit 入账。" if rep.ok else "有错误，改好后再导入。")
        return 0 if rep.ok else 1
    finally:
        await svc.aclose()


async def daily(s: Settings, tenant_id: str, login: str, day: str, *, send: bool) -> int:
    from fos.governance.principal import principal_for_user
    from fos.reports.daily import daily_report, report_lines

    svc = _svc(s)
    try:
        tenant = svc.registry.get(tenant_id)
        if tenant is None:
            print(f"租户 {tenant_id} 不存在", file=sys.stderr)
            return 1
        tz = ZoneInfo(tenant.timezone)
        d = date.fromisoformat(day) if day else utcnow().astimezone(tz).date()
        if send:
            from fos.channels.dingtalk import DingTalkAdapter, DingTalkApp
            from fos.reports.schedule import send_daily

            app = DingTalkApp.from_env(tenant_id)
            if app is None or "dingtalk" not in tenant.im:
                print("没有可用的钉钉凭据，不能推送", file=sys.stderr)
                return 1
            adapter = DingTalkAdapter(app)
            try:
                r = await send_daily(svc, tenant_id, adapter, d)
            finally:
                await adapter.aclose()
            if r.duplicate:
                print(f"{d} 的日报已经推送过")
            print(f"发送：{'、'.join(r.sent) or '无'}")
            for k, v in {**r.skipped, **r.errors}.items():
                print(f"  ! {k}：{v}")
            return 0
        async with svc.sessionmaker() as session:
            u = (
                await session.scalars(select(User).where(User.tenant_id == tenant_id, User.login == login))
            ).one_or_none()
            if u is None:
                print(f"用户 {login} 不存在", file=sys.stderr)
                return 1
            rep = await daily_report(
                session,
                onto=svc.ontologies[tenant_id],
                cfg=svc.dailies[tenant_id],
                trace=svc.traces.get(tenant_id),
                principal=principal_for_user(u, tenant),
                tz=tz,
                day=d,
            )
        mock = "（模拟数据）" if rep["data_mode"] == "mock" else ""
        print(f"{tenant.name} 生产日报 {rep['date']}{mock}，共 {rep['events']} 条记录")
        for ln in report_lines(rep):
            print(f"  {ln}")
        return 0
    finally:
        await svc.aclose()


async def check(s: Settings, tenant_id: str) -> int:
    from fos.onboarding.check import pilot_check

    items = await pilot_check(s, tenant_id)
    for it in items:
        print(it.line())
    fails = sum(1 for i in items if i.level == "fail")
    warns = sum(1 for i in items if i.level == "warn")
    print(f"\n阻断 {fails} 项，提醒 {warns} 项。" + ("可以上线试运行。" if not fails else "先处理 ✗ 项。"))
    return 1 if fails else 0
