"""按租户 LLM Token 预算租约核算、API 查询与 AgentRuntime 超额硬熔断测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fos.config import Settings
from fos.db.models import Run, Thread, Trigger, User
from fos.governance.budget import evaluate_budget
from fos.quickstart import init_db
from fos.services import Services


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_evaluate_budget_deterministic_states() -> None:
    """验证预算租约的 ok / warn / exceeded 及 0=不限 确定性判定逻辑。"""
    ok = evaluate_budget(
        tenant_id="luneng",
        daily_used=100,
        daily_limit=1000,
        monthly_used=500,
        monthly_limit=10000,
        warn_ratio=0.8,
    )
    assert ok["status"] == "ok" and ok["allowed"] is True and ok["daily_pct"] == 10.0

    warn = evaluate_budget(
        tenant_id="luneng",
        daily_used=850,
        daily_limit=1000,
        monthly_used=850,
        monthly_limit=10000,
        warn_ratio=0.8,
    )
    assert warn["status"] == "warn" and warn["allowed"] is True

    ex_d = evaluate_budget(
        tenant_id="luneng",
        daily_used=1000,
        daily_limit=1000,
        monthly_used=1000,
        monthly_limit=10000,
        warn_ratio=0.8,
    )
    assert ex_d["status"] == "exceeded" and ex_d["allowed"] is False and "日度" in ex_d["reason"]

    ex_m = evaluate_budget(
        tenant_id="luneng",
        daily_used=100,
        daily_limit=1000,
        monthly_used=10000,
        monthly_limit=10000,
        warn_ratio=0.8,
    )
    assert ex_m["status"] == "exceeded" and ex_m["allowed"] is False and "月度" in ex_m["reason"]

    unlim = evaluate_budget(
        tenant_id="luneng",
        daily_used=9_999_999,
        daily_limit=0,
        monthly_used=9_999_999,
        monthly_limit=0,
        warn_ratio=0.8,
    )
    assert unlim["status"] == "ok" and unlim["allowed"] is True


def test_ops_budget_api_rbac_and_response(client, keys) -> None:
    """验证 GET /v1/ops/budget 权限隔离（403）及管理员查询返回完整预算快照。"""
    assert client.get("/v1/ops/budget", headers=_h(keys, "sales")).status_code == 403
    r = client.get("/v1/ops/budget", headers=_h(keys, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "luneng"
    assert body["status"] == "ok" and body["allowed"] is True
    assert body["daily_limit"] > 0 and body["monthly_limit"] > 0


@pytest.mark.asyncio
async def test_agent_runtime_budget_circuit_breaker(tmp_path: Path) -> None:
    """当租户当日 Token 用量达到上限时，AgentRuntime 必须硬熔断并拒绝调用 LLM。"""
    db_file = tmp_path / "budget_cb.db"
    s = Settings(database_url=f"sqlite+aiosqlite:///{db_file}")
    await init_db(s)
    svc = Services.build(s)
    try:
        tenant = svc.registry.get("luneng")
        assert tenant is not None
        tenant.budget.daily_tokens = 500

        async with svc.sessionmaker() as session, session.begin():
            u = User(tenant_id="luneng", login="gm_b", display_name="总经理", roles=["gm"], im_ids={})
            session.add(u)
            await session.flush()
            th = Thread(tenant_id="luneng", key="mock:direct:conv_b", kind="im")
            session.add(th)
            await session.flush()
            t_old = Trigger(
                tenant_id="luneng",
                thread_id=th.id,
                source="mock",
                external_id="msg_old",
                sender_ref="gm_b",
                user_id=u.id,
                kind="text",
                route="router",
                status="done",
                payload={"text": "旧请求", "conversation_id": "conv_b", "conversation_type": "direct"},
            )
            t_new = Trigger(
                tenant_id="luneng",
                thread_id=th.id,
                source="mock",
                external_id="msg_new",
                sender_ref="gm_b",
                user_id=u.id,
                kind="text",
                route="router",
                status="pending",
                payload={"text": "查高铝砖成本", "conversation_id": "conv_b", "conversation_type": "direct"},
            )
            session.add_all([t_old, t_new])
            await session.flush()
            session.add(
                Run(
                    tenant_id="luneng",
                    thread_id=th.id,
                    trigger_id=t_old.id,
                    user_id=u.id,
                    skill_id="ask_data",
                    skill_version=1,
                    status="done",
                    task="旧请求",
                    data_mode="real",
                    prompt_tokens=300,
                    completion_tokens=250,
                )
            )
            trig_id = t_new.id

        async with svc.sessionmaker() as session, session.begin():
            out = await svc.runtime._process(session, trig_id)
            trig = await session.get(Trigger, trig_id)
            assert out.decision == "budget_exceeded"
            assert trig is not None and trig.status == "rejected"
            assert any("预算已达上限" in rep for rep in out.replies)
    finally:
        await svc.aclose()
