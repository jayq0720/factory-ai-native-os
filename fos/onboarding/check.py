"""上线检查（W13）：`fos pilot-check <租户>` 逐项列出试点上线前还差什么。

只读：不改任何数据。结果分三级：
- ✓ 就绪；
- ! 提醒：能用，但上线前最好处理（模拟模式、没绑定 IM 的用户、还没导入的主数据……）；
- ✗ 阻断：不处理会出问题（配置不合法、数据库没迁移到最新、审批人角色没有人……）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select, text

from fos.config import Settings

Level = Literal["ok", "warn", "fail"]
ICON = {"ok": "✓", "warn": "!", "fail": "✗"}


@dataclass
class CheckItem:
    level: Level
    title: str
    detail: str = ""

    def line(self) -> str:
        return f"{ICON[self.level]} {self.title}" + (f"：{self.detail}" if self.detail else "")


def _alembic_head() -> str | None:
    ini = Path("alembic.ini")
    if not ini.exists():
        return None
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(ini))).get_current_head()


async def pilot_check(settings: Settings, tenant_id: str) -> list[CheckItem]:
    from fos.channels.dingtalk import DingTalkApp
    from fos.db.models import ChannelIdentity, ObjectState, User
    from fos.services import Services

    out: list[CheckItem] = []
    try:
        svc = Services.build(settings)
    except Exception as e:  # 本体、审批规则、Schema、日报、导入配置任何一个不合法
        return [CheckItem("fail", "配置校验", str(e)[:800])]
    try:
        tenant = svc.registry.get(tenant_id)
        if tenant is None:
            return [CheckItem("fail", "租户", f"{tenant_id} 不存在（有：{', '.join(svc.registry.ids())}）")]
        onto = svc.ontologies[tenant_id]
        out.append(
            CheckItem(
                "ok", "配置校验", f"本体 {' → '.join(onto.layers)}；审批规则、Schema、日报、导入配置一致"
            )
        )
        out.append(
            CheckItem("ok", "运行环境", "FOS_ENV=prod")
            if settings.env == "prod"
            else CheckItem("warn", "运行环境", f"FOS_ENV={settings.env}，模拟信道开着；正式环境设为 prod")
        )

        # ---- 模型与 OCR ----
        if settings.llm_mode.value == "mock":
            out.append(
                CheckItem(
                    "warn", "模型网关", "模拟模式：自然语言问答、模型抽取不可用（填百炼 Key 后切 litellm）"
                )
            )
        elif not settings.litellm_api_key:
            out.append(CheckItem("warn", "模型网关", "litellm 模式但没有设置 FOS_LITELLM_API_KEY"))
        else:
            out.append(CheckItem("ok", "模型网关", f"litellm（{settings.litellm_base_url}）"))
        if settings.ocr_mode.value == "mock":
            out.append(CheckItem("warn", "OCR", "模拟模式：照片录单只能演示，结果是模拟数据，不能入正式账"))
        else:
            out.append(CheckItem("ok", "OCR", f"{settings.ocr_mode.value}（用 fos ocr-check 测连通）"))

        # ---- IM ----
        usable = []
        for im in tenant.im:
            if im == "dingtalk":
                if DingTalkApp.from_env(tenant_id) is not None:
                    usable.append(im)
                    out.append(CheckItem("ok", "钉钉", "凭据已配置（fos channels run 启动长连接）"))
                else:
                    out.append(
                        CheckItem(
                            "warn",
                            "钉钉",
                            f"没有 FOS_DINGTALK_{tenant_id.upper()}_CLIENT_ID / _CLIENT_SECRET",
                        )
                    )
            else:
                name = {"wecom": "企业微信", "feishu": "飞书"}[im]
                out.append(CheckItem("warn", name, "适配器还没做（方案 W19–W22），现在只能用钉钉或网页"))
        if not usable:
            out.append(
                CheckItem("warn", "IM 信道", "没有可用的 IM，工人拍照录单要等 IM 接好；可以先用网页控制台")
            )

        async with svc.sessionmaker() as session:
            # ---- 数据库 ----
            try:
                await session.execute(text("SELECT 1"))
            except Exception as e:
                out.append(CheckItem("fail", "数据库", f"连不上：{str(e)[:200]}"))
                return out
            if settings.database_url.startswith("sqlite"):
                out.append(CheckItem("warn", "数据库", "SQLite 只供本地试用，正式环境用 PostgreSQL（RDS）"))
            else:
                head = await asyncio.to_thread(_alembic_head)
                try:  # 保存点：表不存在时不让整个事务作废
                    async with session.begin_nested():
                        q = text("SELECT version_num FROM alembic_version")
                        cur = (await session.execute(q)).scalar()
                except Exception:
                    cur = None
                if head and cur != head:
                    out.append(
                        CheckItem(
                            "fail", "数据库迁移", f"当前 {cur or '未迁移'}，最新 {head}：执行 fos init-db"
                        )
                    )
                else:
                    out.append(CheckItem("ok", "数据库", f"PostgreSQL，迁移版本 {cur}"))

            # ---- 用户与绑定 ----
            users = (
                await session.scalars(
                    select(User).where(User.tenant_id == tenant_id, User.is_active.is_(True))
                )
            ).all()
            bound = set(
                (
                    await session.scalars(
                        select(ChannelIdentity.user_id).where(ChannelIdentity.tenant_id == tenant_id)
                    )
                ).all()
            )
            roles_with_people = {r for u in users for r in u.roles}
            if "admin" not in roles_with_people:
                out.append(
                    CheckItem(
                        "fail", "管理员", "没有管理员账号：fos quickstart 或 fos create-user --roles admin"
                    )
                )
            else:
                out.append(CheckItem("ok", "用户", f"{len(users)} 个启用的用户"))
            unbound = [u.login for u in users if u.id not in bound and u.login != "admin"]
            if unbound:
                more = " 等" if len(unbound) > 8 else ""
                out.append(
                    CheckItem(
                        "warn",
                        "IM 绑定",
                        f"{len(unbound)} 个用户没绑定 IM（{'、'.join(unbound[:8])}{more}）：fos bind-im",
                    )
                )
            pol = svc.policies.get(tenant_id)
            need = sorted({s.role for r in (pol.rules.values() if pol else []) for s in r.steps if s.role})
            empty = [f"{r}（{tenant.roles[r].name}）" for r in need if r not in roles_with_people]
            if empty:
                out.append(
                    CheckItem(
                        "fail", "审批人", f"这些角色在审批规则里但没有人：{'、'.join(empty)}，审批会卡住"
                    )
                )
            elif need:
                out.append(CheckItem("ok", "审批人", f"审批规则用到的角色都有人：{'、'.join(need)}"))

            # ---- 主数据 ----
            async def count(obj_type: str, mode: str) -> int:
                q = (
                    select(func.count())
                    .select_from(ObjectState)
                    .where(
                        ObjectState.tenant_id == tenant_id,
                        ObjectState.object_type == obj_type,
                        ObjectState.data_mode == mode,
                    )
                )
                return int((await session.execute(q)).scalar_one())

            cfg = svc.imports[tenant_id]
            have, lack = [], []
            for s in cfg.sheets:
                t = cfg.object_of(onto, s)
                n = await count(t, "real")
                (have if n else lack).append(f"{s.name} {n}" if n else s.name)
            if lack:
                out.append(
                    CheckItem(
                        "warn",
                        "主数据",
                        f"还没有：{'、'.join(lack)}（导入模板：网页 /app/pilot.html 或 fos import-template）"
                        + (f"；已有：{'、'.join(have)}" if have else ""),
                    )
                )
            else:
                out.append(CheckItem("ok", "主数据", "、".join(have)))
            mock = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(ObjectState)
                        .where(ObjectState.tenant_id == tenant_id, ObjectState.data_mode == "mock")
                    )
                ).scalar_one()
            )
            if mock:
                out.append(
                    CheckItem(
                        "warn",
                        "演示数据",
                        f"库里有 {mock} 条模拟数据（演示用，不影响真实数据，正式库建议不导入演示数据）",
                    )
                )
            if svc.engineering.get(tenant_id) is not None and "price_item" in onto.objects:
                n = await count("price_item", "real")
                out.append(
                    CheckItem("ok", "工程知识库", f"真实价格 {n} 条")
                    if n
                    else CheckItem(
                        "warn", "工程知识库", "没有真实价格，报价只能用演示知识库（模拟数据，不能发布）"
                    )
                )

            # ---- 日报 ----
            sched = tenant.reports.daily
            if sched is None or not sched.to:
                out.append(
                    CheckItem("warn", "生产日报", "没有设置定时推送收报人（tenant.yaml reports.daily.to）")
                )
            else:
                logins = {u.login: u for u in users}
                bad = [x for x in sched.to if x not in logins or logins[x].id not in bound]
                out.append(
                    CheckItem(
                        "warn", "生产日报", f"{'、'.join(bad)} 不存在或没绑定 IM，收不到 {sched.at} 的日报"
                    )
                    if bad
                    else CheckItem("ok", "生产日报", f"每天 {sched.at} 推送给 {'、'.join(sched.to)}")
                )
    finally:
        await svc.aclose()
    return out
