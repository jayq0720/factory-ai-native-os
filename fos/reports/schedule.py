"""定时 Trigger：每天按 tenant.yaml 的 reports.daily 推送生产日报（方案 2.4.2「日报定时 Trigger」）。

- 由 `fos channels run` 进程里的循环按点触发，也可以用 `fos daily-report <租户> --send` 手动 / cron 触发；
- 每次推送记一条 Trigger（source=schedule，external_id=daily_report:<日期>），唯一约束保证同一天只推一次，
  多个进程同时跑也不会重复发；
- 每个收报人按自己的权限出一份报告，发到他绑定的 IM 单聊；没绑定的跳过并记在 Trigger 结果里；
- 先提交再发送（规则 6：回复在事务提交后再发）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from fos.channels.base import ChannelAdapter, ChannelError
from fos.channels.cards import Card
from fos.channels.types import ConversationRef
from fos.db.models import ChannelIdentity, Thread, Trigger, User, utcnow
from fos.governance.principal import principal_for_user
from fos.reports.daily import daily_report, report_card

if TYPE_CHECKING:
    from fos.services import Services

log = logging.getLogger(__name__)
THREAD_KEY = "schedule:daily_report"


@dataclass
class SendResult:
    day: date
    duplicate: bool = False
    sent: list[str] = field(default_factory=list)  # 登录名
    skipped: dict[str, str] = field(default_factory=dict)  # 登录名 → 原因
    errors: dict[str, str] = field(default_factory=dict)


async def _thread(session, tenant_id: str) -> str:
    q = select(Thread.id).where(Thread.tenant_id == tenant_id, Thread.key == THREAD_KEY)
    tid = (await session.scalars(q)).one_or_none()
    if tid:
        return tid
    th = Thread(tenant_id=tenant_id, key=THREAD_KEY, kind="schedule")
    try:
        async with session.begin_nested():
            session.add(th)
        return th.id
    except IntegrityError:
        return (await session.scalars(q)).one()


async def send_daily(
    svc: Services, tenant_id: str, adapter: ChannelAdapter, day: date, *, force: bool = False
) -> SendResult:
    tenant = svc.registry.get(tenant_id)
    assert tenant is not None
    sched = tenant.reports.daily
    res = SendResult(day)
    if sched is None:
        return res
    tz = ZoneInfo(tenant.timezone)
    outbox: list[tuple[str, ConversationRef, Card]] = []
    async with svc.sessionmaker() as session, session.begin():
        ext = f"daily_report:{day.isoformat()}" + (f":{utcnow().timestamp():.0f}" if force else "")
        trig = Trigger(
            tenant_id=tenant_id,
            source="schedule",
            external_id=ext,
            thread_id=await _thread(session, tenant_id),
            sender_ref="system",
            kind="schedule",
            route="daily_report",
            status="pending",
            payload={"date": day.isoformat(), "to": list(sched.to), "channel": adapter.name},
            created_at=utcnow(),
        )
        try:
            async with session.begin_nested():
                session.add(trig)
        except IntegrityError:
            res.duplicate = True
            return res
        for login in sched.to:
            u = (
                await session.scalars(select(User).where(User.tenant_id == tenant_id, User.login == login))
            ).one_or_none()
            if u is None or not u.is_active:
                res.skipped[login] = "用户不存在或已停用"
                continue
            ident = (
                await session.scalars(
                    select(ChannelIdentity).where(
                        ChannelIdentity.tenant_id == tenant_id,
                        ChannelIdentity.channel == adapter.name,
                        ChannelIdentity.user_id == u.id,
                    )
                )
            ).first()
            if ident is None:
                res.skipped[login] = f"没有绑定 {adapter.name} 账号"
                continue
            rep = await daily_report(
                session,
                onto=svc.ontologies[tenant_id],
                cfg=svc.dailies[tenant_id],
                trace=svc.traces.get(tenant_id),
                principal=principal_for_user(u, tenant),
                tz=tz,
                day=day,
            )
            to = ConversationRef(
                channel=adapter.name,  # type: ignore[arg-type]
                conversation_id=ident.external_id,
                conversation_type="direct",
                user_id=ident.external_id,
            )
            outbox.append((login, to, report_card(rep)))
        trig.status = "done"
        trig.result = {"recipients": [x[0] for x in outbox], "skipped": res.skipped}
    for login, to, card in outbox:  # 提交之后再发
        try:
            await adapter.send_card(to, card, card_id="", short_code="")
            res.sent.append(login)
        except ChannelError as e:
            log.warning("日报发送失败 %s/%s：%s", tenant_id, login, e)
            res.errors[login] = str(e)[:200]
    return res


def next_fire(now: datetime, at: str, tz: ZoneInfo) -> datetime:
    """下一次推送时间（工厂时区的钟点）。"""
    h, m = (int(x) for x in at.split(":"))
    local = now.astimezone(tz)
    fire = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if fire <= local:
        fire += timedelta(days=1)
    return fire


async def daily_loop(svc: Services, tenant_id: str, adapter: ChannelAdapter) -> None:
    """常驻循环：到点推送当天日报。进程重启错过的不补发（第二天照常）。"""
    tenant = svc.registry.get(tenant_id)
    if tenant is None or tenant.reports.daily is None or not tenant.reports.daily.to:
        return
    tz = ZoneInfo(tenant.timezone)
    while True:
        fire = next_fire(utcnow(), tenant.reports.daily.at, tz)
        await asyncio.sleep(max(1.0, (fire - utcnow()).total_seconds()))
        try:
            r = await send_daily(svc, tenant_id, adapter, fire.date())
            log.info("%s 日报 %s：发送 %s，跳过 %s", tenant_id, r.day, r.sent, r.skipped)
        except Exception:  # 一次失败不影响第二天
            log.exception("%s 日报推送失败", tenant_id)
