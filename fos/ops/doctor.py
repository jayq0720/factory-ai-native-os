"""云端控制面一键上线前/运行期诊断引擎（对标 fos-edge doctor）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.config import Settings
from fos.db.models import Event, User
from fos.governance.budget import check_tenant_budget
from fos.ops.packs import list_packs
from fos.ops.service import security_baseline, verify_tenant_replication
from fos.tenancy import TenantRegistry

if TYPE_CHECKING:
    from fos.services import Services

Status = Literal["ok", "warn", "fail"]


@dataclass(slots=True)
class DoctorCheck:
    name: str
    status: Status
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _check_env(settings: Settings) -> DoctorCheck:
    warns: list[str] = []
    if settings.env == "prod":
        if settings.rate_limit_per_min <= 0:
            warns.append("生产环境关闭了 API 限流（FOS_RATE_LIMIT_PER_MIN=0）")
        if not settings.edge_command_key.strip():
            warns.append("未配置 FOS_EDGE_COMMAND_KEY，无法向厂端网关下发签名命令")
        if settings.llm_mode == "mock":
            warns.append("FOS_LLM_MODE=mock")
        if settings.ocr_mode == "mock":
            warns.append("FOS_OCR_MODE=mock")
    base = (
        f"env={settings.env}, llm={settings.llm_mode}, ocr={settings.ocr_mode}, "
        f"rate_limit={settings.rate_limit_per_min}/min"
    )
    if warns:
        return DoctorCheck("env_profile", "warn", f"{base}；提示：{'；'.join(warns)}")
    return DoctorCheck("env_profile", "ok", base)


def _check_blob_dir(settings: Settings) -> DoctorCheck:
    target = settings.blob_dir
    if target.exists():
        if target.is_dir():
            return DoctorCheck("blob_dir", "ok", f"原件目录可用：{target}")
        return DoctorCheck("blob_dir", "fail", f"原件路径不是目录：{target}")
    parent = target.parent
    if parent.exists() and parent.is_dir():
        return DoctorCheck("blob_dir", "warn", f"原件目录尚未创建（启动时自动建）：{target}")
    return DoctorCheck("blob_dir", "warn", f"原件目录父级不存在：{target}")


def _check_packs(settings: Settings) -> DoctorCheck:
    packs = list_packs(settings.packs_dir)
    if not packs:
        return DoctorCheck("packs", "fail", f"未发现任何行业包：{settings.packs_dir}")
    bad = [p["pack"] for p in packs if not p.get("ok")]
    names = ", ".join(p["pack"] for p in packs)
    if bad:
        return DoctorCheck("packs", "fail", f"行业包校验失败：{', '.join(bad)}（全部：{names}）")
    total_cases = sum(int(p.get("eval_cases", 0)) for p in packs)
    return DoctorCheck("packs", "ok", f"{len(packs)} 个行业包就绪（{names}），评测用例 {total_cases} 条")


def _check_tenants_offline(settings: Settings) -> tuple[DoctorCheck, TenantRegistry | None]:
    try:
        reg = TenantRegistry.load(settings.tenants_dir)
    except Exception as e:
        return DoctorCheck("tenants", "fail", f"加载租户注册表失败：{e}"), None
    tids = sorted(reg.ids())
    if not tids:
        return DoctorCheck("tenants", "fail", f"未注册任何租户：{settings.tenants_dir}"), reg
    failed: list[str] = []
    for tid in tids:
        try:
            rep = verify_tenant_replication(
                packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir, tenant_id=tid
            )
            if not rep.get("ready", False):
                failed.append(tid)
        except Exception as e:
            failed.append(f"{tid}({e})")
    if failed:
        return DoctorCheck("tenants", "fail", f"租户配置校验失败：{', '.join(failed)}"), reg
    return DoctorCheck("tenants", "ok", f"{len(tids)} 个租户本体/策略/技能校验通过（{', '.join(tids)}）"), reg


async def _check_database(session: AsyncSession) -> DoctorCheck:
    try:
        ev_cnt = int((await session.execute(select(func.count(Event.id)))).scalar_one() or 0)
        q_usr = select(func.count(User.id)).where(User.is_active.is_(True))
        usr_cnt = int((await session.execute(q_usr)).scalar_one() or 0)
        return DoctorCheck("database", "ok", f"数据库连接正常：账本事件 {ev_cnt} 条，活跃用户 {usr_cnt} 人")
    except Exception as e:
        return DoctorCheck("database", "fail", f"数据库查询失败：{e}")


async def _check_budgets(session: AsyncSession, reg: TenantRegistry) -> DoctorCheck:
    exceeded: list[str] = []
    warned: list[str] = []
    checked = 0
    for tid in sorted(reg.ids()):
        t = reg.get(tid)
        if t is None:
            continue
        b = await check_tenant_budget(session, t)
        checked += 1
        if not b["allowed"]:
            exceeded.append(f"{tid}({b['reason']})")
        elif b["status"] == "warn":
            warned.append(f"{tid}(日{b['daily_pct']}%/月{b['monthly_pct']}%)")
    if exceeded:
        return DoctorCheck("budgets", "fail", f"租户 Token 预算已熔断：{', '.join(exceeded)}")
    if warned:
        return DoctorCheck("budgets", "warn", f"租户 Token 预算接近上限：{', '.join(warned)}")
    return DoctorCheck("budgets", "ok", f"{checked} 个租户 Token 预算租约均在安全水位")


async def _check_edge_fleet(session: AsyncSession, reg: TenantRegistry) -> DoctorCheck:
    from fos.ops.edge_fleet import tenant_edge_alarm_counts

    stats = await tenant_edge_alarm_counts(session, list(reg.ids()))
    total_gw = sum(int(x["gateways_total"]) for x in stats)
    online_gw = sum(int(x["gateways_online"]) for x in stats)
    open_alarms = sum(int(x["open_alarms_total"]) for x in stats)
    if total_gw == 0 and open_alarms == 0:
        return DoctorCheck("edge_fleet", "ok", "暂无已接入厂端网关，无未闭环窑炉告警")
    issues: list[str] = []
    if online_gw < total_gw:
        issues.append(f"离线网关 {total_gw - online_gw}/{total_gw} 台")
    if open_alarms > 0:
        issues.append(f"未闭环窑炉告警 {open_alarms} 条")
    if issues:
        return DoctorCheck("edge_fleet", "warn", "；".join(issues))
    return DoctorCheck("edge_fleet", "ok", f"{online_gw}/{total_gw} 台厂端网关在线，无未闭环窑炉告警")


async def run_doctor(
    settings: Settings,
    *,
    session: AsyncSession | None = None,
    services: Services | None = None,
) -> dict[str, Any]:
    """执行云端控制面只读巡检，返回结构化健康报告。"""
    checks: list[DoctorCheck] = [
        _check_env(settings),
        _check_blob_dir(settings),
        _check_packs(settings),
    ]
    reg: TenantRegistry | None = None
    if services is not None:
        reg = services.registry
        tids = sorted(reg.ids())
        warns = [tid for tid in tids if not security_baseline(services, tid).get("passed", False)]
        if not tids:
            checks.append(DoctorCheck("tenants", "fail", "未注册任何租户"))
        elif warns:
            st: Status = "fail" if settings.env == "prod" else "warn"
            checks.append(DoctorCheck("tenants", st, f"{len(tids)} 个租户安全基线待关注：{', '.join(warns)}"))
        else:
            checks.append(DoctorCheck("tenants", "ok", f"{len(tids)} 个租户全部通过（{', '.join(tids)}）"))
    else:
        t_check, reg = _check_tenants_offline(settings)
        checks.append(t_check)

    if session is not None:
        db_check = await _check_database(session)
        checks.append(db_check)
        if db_check.status == "ok" and reg is not None:
            checks.append(await _check_budgets(session, reg))
            checks.append(await _check_edge_fleet(session, reg))

    summary = {
        "ok": sum(1 for c in checks if c.status == "ok"),
        "warn": sum(1 for c in checks if c.status == "warn"),
        "fail": sum(1 for c in checks if c.status == "fail"),
    }
    return {
        "ok": summary["fail"] == 0,
        "env": settings.env,
        "summary": summary,
        "checks": [c.to_dict() for c in checks],
    }


def format_doctor_text(report: dict[str, Any]) -> str:
    """将诊断报告渲染为终端可读中文清单。"""
    lines = [f"FOS 云端控制面自检（env={report['env']}）"]
    for c in report["checks"]:
        tag = c["status"].upper().ljust(4)
        lines.append(f"  [{tag}] {c['name']}: {c['detail']}")
    s = report["summary"]
    verdict = "通过" if report["ok"] else "失败"
    lines.append(f"汇总：{verdict}（ok={s['ok']}, warn={s['warn']}, fail={s['fail']}）")
    return "\n".join(lines)
