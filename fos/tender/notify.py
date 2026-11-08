"""投标截止提醒（方案 4.3 第 2 步、4.8 Watchdog）：`fos channels run` 进程里轮询，
在澄清截止、投标截止前 tender.yaml 配置的时间点（默认 72 / 24 / 3 小时）推给负责人和配置的角色。

Trigger 唯一约束保证每个时间点只推一次；进程停了一段时间再启动，同时跨过的几个时间点只推最近的一条。
先提交再发送（规则 6）；推送只读，不写账本。登记时生成的待办（收件箱）是另一条提醒渠道。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fos.channels.base import ChannelAdapter, ChannelError
from fos.db.models import ChannelIdentity, ObjectState, Trigger, User, utcnow
from fos.planning.notify import _thread, dm, role_identities
from fos.tender import service as ts
from fos.tender.cards import reminder_text
from fos.tender.config import TenderConfig

if TYPE_CHECKING:
    from fos.services import Services

log = logging.getLogger(__name__)
DEADLINES = (("clarification_deadline", "澄清 / 质疑截止"), ("bid_deadline", "投标截止"))


def due_offsets(deadline: datetime, now: datetime, offsets: list[float]) -> list[float]:
    """已经跨过、但还没到截止的提醒时间点（小时），从近到远。"""
    if now >= deadline:
        return []
    return sorted(h for h in offsets if now >= deadline - timedelta(hours=h))


async def _owner_identities(
    session: AsyncSession, tenant_id: str, login: str | None, channel: str
) -> list[ChannelIdentity]:
    if not login:
        return []
    u = (
        await session.scalars(select(User).where(User.tenant_id == tenant_id, User.login == login))
    ).one_or_none()
    if u is None or not u.is_active:
        return []
    rows = await session.scalars(
        select(ChannelIdentity).where(
            ChannelIdentity.tenant_id == tenant_id,
            ChannelIdentity.user_id == u.id,
            ChannelIdentity.channel == channel,
        )
    )
    return list(rows)


async def remind(
    svc: Services, tenant_id: str, adapter: ChannelAdapter, now: datetime | None = None
) -> list[str]:
    """推送到点的截止提醒。返回推送的 “招标项目:截止类型:小时”。"""
    cfg: TenderConfig | None = svc.tenders.get(tenant_id)
    if cfg is None:
        return []
    now = now or utcnow()
    outbox: list[tuple] = []
    done: list[str] = []
    async with svc.sessionmaker() as session, session.begin():
        rows = await session.scalars(
            select(ObjectState).where(ObjectState.tenant_id == tenant_id, ObjectState.object_type == "tender")
        )
        tenders = [o for o in rows if o.state.get("status") == "open"]
        if not tenders:
            return []
        thread_id = await _thread(session, tenant_id)
        roles = await role_identities(session, tenant_id, cfg.reminders.notify_roles, adapter.name)
        for o in tenders:
            for key, label in DEADLINES:
                if not o.state.get(key):
                    continue
                due = datetime.fromisoformat(o.state[key])
                crossed = due_offsets(due, now, cfg.reminders.offsets_hours)
                if not crossed:
                    continue
                fresh = []
                for h in crossed:
                    trig = Trigger(
                        tenant_id=tenant_id,
                        source="schedule",
                        external_id=f"tender_remind:{o.object_id}:{key}:{due.isoformat()}:{h:g}"[:160],
                        thread_id=thread_id,
                        sender_ref="system",
                        kind="schedule",
                        route="tender_remind",
                        status="done",
                        payload={"tender": o.object_id, "deadline": key, "hours": h},
                        created_at=utcnow(),
                    )
                    try:
                        async with session.begin_nested():
                            session.add(trig)
                        fresh.append(h)
                    except IntegrityError:
                        continue
                if not fresh or min(fresh) != crossed[0]:
                    continue  # 最近的时间点已经推过
                a = await ts.latest(session, tenant_id, o.object_id)
                left = (due - now).total_seconds() / 3600
                text = reminder_text(ts.tender_out(o), label, due, left, ts.summary(a) if a else None)
                to = {
                    i.external_id: i
                    for i in await _owner_identities(session, tenant_id, o.state.get("owner"), adapter.name)
                }
                to.update({i.external_id: i for _, i in roles})
                outbox += [(dm(i), text) for i in to.values()]
                done.append(f"{o.object_id}:{key}:{crossed[0]:g}")
    for ref, text in outbox:  # 提交之后再发
        try:
            await adapter.send_text(ref, text)
        except ChannelError as err:
            log.warning("投标截止提醒推送失败：%s", err)
    return done


async def tender_watch_loop(svc: Services, tenant_id: str, adapter: ChannelAdapter) -> None:
    cfg = svc.tenders.get(tenant_id)
    if cfg is None:
        return
    while True:
        try:
            done = await remind(svc, tenant_id, adapter)
            if done:
                log.info("%s 投标截止提醒：%s", tenant_id, done)
        except Exception:  # 一次失败不影响下一轮
            log.exception("%s 投标截止提醒失败", tenant_id)
        await asyncio.sleep(cfg.reminders.poll_minutes * 60)
