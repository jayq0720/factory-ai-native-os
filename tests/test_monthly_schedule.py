"""月度经营报表定时推送（幂等去重、权限过滤、先提交再发）与 IM 绑定覆盖率接口测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fos.channels.mock import MockChannel
from fos.config import Settings
from fos.db.models import ChannelIdentity, User
from fos.quickstart import init_db
from fos.reports.schedule import next_monthly_fire, prev_month_str, send_monthly
from fos.services import Services
from fos.tenancy.config import MonthlyReportSchedule


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_prev_month_and_next_monthly_fire() -> None:
    """验证跨年月份推算与工厂时区下一次月报触发时间计算。"""
    assert prev_month_str(date(2026, 1, 1)) == "2025-12"
    assert prev_month_str(date(2026, 11, 1)) == "2026-10"

    tz = ZoneInfo("Asia/Shanghai")
    # 上海时间 2026-12-01 08:00 -> 当天 09:00 触发
    t1 = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    f1 = next_monthly_fire(t1, 1, "09:00", tz)
    assert (f1.year, f1.month, f1.day, f1.hour) == (2026, 12, 1, 9)

    # 上海时间 2026-12-01 10:00 -> 跨年到 2027-01-01 09:00 触发
    t2 = datetime(2026, 12, 1, 2, 0, tzinfo=UTC)
    f2 = next_monthly_fire(t2, 1, "09:00", tz)
    assert (f2.year, f2.month, f2.day, f2.hour) == (2027, 1, 1, 9)


@pytest.mark.asyncio
async def test_send_monthly_idempotent_and_rbac_filtered(tmp_path: Path) -> None:
    """验证 send_monthly 幂等防重推、权限校验、未绑定跳过与卡片发送。"""
    db_file = tmp_path / "monthly_sched.db"
    s = Settings(database_url=f"sqlite+aiosqlite:///{db_file}")
    await init_db(s)
    svc = Services.build(s)
    try:
        tenant = svc.registry.get("luneng")
        assert tenant is not None
        tenant.reports.monthly = MonthlyReportSchedule(
            day_of_month=1, at="09:00", to=["gm", "warehouse", "sales", "missing_u"]
        )
        async with svc.sessionmaker() as session, session.begin():
            u_gm = User(tenant_id="luneng", login="gm", display_name="总经理", roles=["gm"], im_ids={})
            u_wh = User(
                tenant_id="luneng", login="warehouse", display_name="仓管", roles=["warehouse"], im_ids={}
            )
            u_sl = User(tenant_id="luneng", login="sales", display_name="销售", roles=["sales"], im_ids={})
            session.add_all([u_gm, u_wh, u_sl])
            await session.flush()
            session.add_all(
                [
                    ChannelIdentity(tenant_id="luneng", channel="mock", external_id="im_gm", user_id=u_gm.id),
                    ChannelIdentity(tenant_id="luneng", channel="mock", external_id="im_wh", user_id=u_wh.id),
                ]
            )

        ch = MockChannel()
        r1 = await send_monthly(svc, "luneng", ch, "2026-11")
        assert r1.duplicate is False
        assert r1.sent == ["gm"]
        assert "market.lead:read" in r1.skipped["warehouse"]
        assert "没有绑定" in r1.skipped["sales"]
        assert "不存在" in r1.skipped["missing_u"]
        assert len(ch.outbox) == 1
        assert "2026-11 经营月报" in ch.outbox[0]["card"]["title"]

        # 同月重复触发必须被 Trigger 唯一约束幂等拦下
        r2 = await send_monthly(svc, "luneng", ch, "2026-11")
        assert r2.duplicate is True
        assert len(ch.outbox) == 1
    finally:
        await svc.aclose()


def test_ops_im_bindings_coverage_api(client, keys) -> None:
    """验证 GET /v1/ops/im-bindings 权限隔离与全厂 IM 绑定覆盖率统计。"""
    assert client.get("/v1/ops/im-bindings", headers=_h(keys, "sales")).status_code == 403

    r0 = client.get("/v1/ops/im-bindings", headers=_h(keys, "admin"))
    assert r0.status_code == 200
    b0 = r0.json()
    assert b0["active_users"] >= 5 and b0["bound_active_users"] == 0 and b0["coverage_pct"] == 0.0

    assert (
        client.put(
            "/v1/channels/identities",
            json={"channel": "dingtalk", "external_id": "dt_gm_001", "login": "gm"},
            headers=_h(keys, "admin"),
        ).status_code
        == 200
    )
    r1 = client.get("/v1/ops/im-bindings", headers=_h(keys, "admin"))
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["bound_active_users"] == 1 and b1["coverage_pct"] > 0.0
    gm_row = next(it for it in b1["items"] if it["login"] == "gm")
    assert gm_row["bound"] is True
    assert gm_row["bindings"] == [{"channel": "dingtalk", "external_id": "dt_gm_001"}]
