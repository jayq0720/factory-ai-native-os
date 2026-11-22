"""SaaS 多租户运营监控、安全基线自检与新工厂复制验证服务（W28）。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Approval, Event, ObjectState, Run, Todo, Trigger, User, utcnow
from fos.ontology.loader import load_ontology
from fos.policy import load_policy
from fos.skills.loader import load_skills
from fos.tenancy import TenantRegistry
from fos.tools.registry import REGISTRY

if TYPE_CHECKING:
    from fos.services import Services


def enabled_modules(svc: Services, tenant_id: str) -> list[str]:
    """列出指定租户已启用的业务模块清单。"""
    checks = (
        ("orders", tenant_id in svc.orders),
        ("planning", tenant_id in svc.planning),
        ("supply", tenant_id in svc.supply),
        ("engineering", svc.engineering.get(tenant_id) is not None),
        ("tender", tenant_id in svc.tenders),
        ("monitoring", tenant_id in svc.monitoring),
        ("quality", tenant_id in svc.quality),
        ("finance", tenant_id in svc.finance),
        ("eam", tenant_id in svc.eam),
        ("ehs", tenant_id in svc.ehs),
        ("hr", tenant_id in svc.hr),
        ("rnd", tenant_id in svc.rnd),
        ("construction", tenant_id in svc.construction),
        ("market", tenant_id in svc.market),
    )
    return [name for name, ok in checks if ok]


async def tenant_health_snapshot(
    session: AsyncSession, svc: Services, tenant_id: str, today: date
) -> dict[str, Any]:
    """确定性采集单租户活跃度、账本规模、待办审批与近 7 天 Token 用量。"""
    tenant = svc.registry.get(tenant_id)
    if tenant is None:
        raise ValueError(f"租户 {tenant_id} 不存在")
    since = utcnow() - timedelta(days=7)
    users_active = int(
        (
            await session.scalar(
                select(func.count()).select_from(User).where(User.tenant_id == tenant_id, User.is_active)
            )
        )
        or 0
    )
    events_total = int(
        (await session.scalar(select(func.count()).select_from(Event).where(Event.tenant_id == tenant_id)))
        or 0
    )
    events_7d = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.tenant_id == tenant_id, Event.occurred_at >= since)
            )
        )
        or 0
    )
    objects_total = int(
        (
            await session.scalar(
                select(func.count()).select_from(ObjectState).where(ObjectState.tenant_id == tenant_id)
            )
        )
        or 0
    )
    open_todos = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(Todo)
                .where(Todo.tenant_id == tenant_id, Todo.status == "open")
            )
        )
        or 0
    )
    pending_approvals = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(Approval)
                .where(Approval.tenant_id == tenant_id, Approval.status == "pending")
            )
        )
        or 0
    )
    triggers_7d = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(Trigger)
                .where(Trigger.tenant_id == tenant_id, Trigger.created_at >= since)
            )
        )
        or 0
    )
    tok_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(Run.prompt_tokens), 0),
                func.coalesce(func.sum(Run.completion_tokens), 0),
            ).where(Run.tenant_id == tenant_id, Run.created_at >= since)
        )
    ).one()
    llm_tokens_7d = int(tok_row[0] or 0) + int(tok_row[1] or 0)

    return {
        "tenant_id": tenant.id,
        "name": tenant.name,
        "industry_pack": tenant.industry_pack,
        "timezone": tenant.timezone,
        "im_channels": list(tenant.im),
        "enabled_modules": enabled_modules(svc, tenant_id),
        "date": today.isoformat(),
        "counts": {
            "users_active": users_active,
            "events_total": events_total,
            "events_7d": events_7d,
            "objects_total": objects_total,
            "open_todos": open_todos,
            "pending_approvals": pending_approvals,
            "triggers_7d": triggers_7d,
            "llm_tokens_7d": llm_tokens_7d,
        },
    }


def security_baseline(svc: Services, tenant_id: str) -> dict[str, Any]:
    """确定性执行租户安全与上线基线自检。"""
    tenant = svc.registry.get(tenant_id)
    if tenant is None:
        raise ValueError(f"租户 {tenant_id} 不存在")
    onto = svc.ontologies.get(tenant_id)
    pol = svc.policies.get(tenant_id)
    skills = svc.skills.get(tenant_id, {})
    env = svc.settings.env
    checks: list[dict[str, Any]] = [
        {
            "id": "ontology_loaded",
            "name": "本体层合并与校验",
            "ok": onto is not None and len(onto.objects) > 0,
            "severity": "error",
            "detail": f"已加载 {len(onto.layers)} 层、{len(onto.objects)} 对象" if onto else "未加载本体",
        },
        {
            "id": "policy_loaded",
            "name": "审批红线策略加载",
            "ok": pol is not None,
            "severity": "warn",
            "detail": f"共 {len(pol.rules)} 条审批/红线规则" if pol else "未配置策略",
        },
        {
            "id": "im_configured",
            "name": "IM 接入信道配置",
            "ok": len(tenant.im) > 0,
            "severity": "warn",
            "detail": "已配置：" + (", ".join(tenant.im) if tenant.im else "无"),
        },
        {
            "id": "edge_signer_prod",
            "name": "厂端 fos-edge 命令签名私钥",
            "ok": (env != "prod") or (tenant_id not in svc.monitoring) or (svc.edge_signer is not None),
            "severity": "error" if env == "prod" else "info",
            "detail": "已配置 Ed25519 私钥" if svc.edge_signer is not None else "未配置 FOS_EDGE_COMMAND_KEY",
        },
        {
            "id": "llm_mock_prod",
            "name": "生产环境禁止模拟 LLM",
            "ok": (env != "prod") or (svc.settings.llm_mode.value != "mock"),
            "severity": "error" if env == "prod" else "info",
            "detail": f"env={env}, llm_mode={svc.settings.llm_mode.value}",
        },
    ]
    passed = all(c["ok"] or c["severity"] != "error" for c in checks)
    return {
        "tenant_id": tenant_id,
        "env": env,
        "skills_count": len(skills),
        "passed": passed,
        "checks": checks,
    }


async def saas_overview(session: AsyncSession, svc: Services, today: date) -> dict[str, Any]:
    """多租户 SaaS 运营总览（仅平台管理员可访问）。"""
    tenants: list[dict[str, Any]] = []
    for tid in sorted(svc.registry.ids()):
        snap = await tenant_health_snapshot(session, svc, tid, today)
        snap["security_passed"] = security_baseline(svc, tid)["passed"]
        tenants.append(snap)
    return {
        "date": today.isoformat(),
        "tenant_count": len(tenants),
        "totals": {
            "users_active": sum(t["counts"]["users_active"] for t in tenants),
            "events_total": sum(t["counts"]["events_total"] for t in tenants),
            "objects_total": sum(t["counts"]["objects_total"] for t in tenants),
            "open_todos": sum(t["counts"]["open_todos"] for t in tenants),
            "pending_approvals": sum(t["counts"]["pending_approvals"] for t in tenants),
            "llm_tokens_7d": sum(t["counts"]["llm_tokens_7d"] for t in tenants),
        },
        "tenants": tenants,
    }


def verify_tenant_replication(*, packs_dir: Path, tenants_dir: Path, tenant_id: str) -> dict[str, Any]:
    """验证任意新工厂租户目录（tenant.yaml + 可选 policy.yaml）可零代码复制并加载完整本体/策略/Skill。"""
    import fos.services  # noqa: F401  确保全部业务工具已注册到 REGISTRY

    reg = TenantRegistry.load(tenants_dir)
    t = reg.get(tenant_id)
    if t is None:
        raise ValueError(f"租户 {tenant_id} 不存在")
    onto = load_ontology(t, packs_dir=packs_dir, tenants_dir=tenants_dir)
    pol = load_policy(t, packs_dir=packs_dir, tenants_dir=tenants_dir, ontology=onto)
    skills = load_skills(t, packs_dir=packs_dir, ontology=onto, tools=set(REGISTRY))
    return {
        "tenant_id": t.id,
        "name": t.name,
        "industry_pack": t.industry_pack,
        "ontology_layers": list(onto.layers),
        "objects_count": len(onto.objects),
        "events_count": len(onto.events),
        "policy_rules_count": len(pol.rules),
        "skills_count": len(skills),
        "ready": True,
    }
