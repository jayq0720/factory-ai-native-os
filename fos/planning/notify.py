"""主动推送：
- 新订单到来 → 自动评估交期，推给计划员
  （`fos channels run` 进程里轮询，Trigger 唯一约束保证每个订单只推一次）；
- 确认排窑计划后有原料缺口 → 推给采购。

先提交再发送（规则 6）；推送只读，不写账本。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fos.channels.base import ChannelAdapter, ChannelError
from fos.channels.types import ConversationRef
from fos.db.models import ChannelIdentity, ObjectState, Thread, Trigger, User, utcnow
from fos.planning.cards import evaluation_card
from fos.planning.service import PlanningError, evaluate

if TYPE_CHECKING:
    from fos.services import Services

log = logging.getLogger(__name__)
THREAD_KEY = "schedule:planning"


async def role_identities(
    session: AsyncSession, tenant_id: str, roles: list[str], channel: str | None = None
) -> list[tuple[User, ChannelIdentity]]:
    """有这些角色之一、绑定了 IM 的在职用户（channel 为空 = 所有信道）。"""
    users = await session.scalars(select(User).where(User.tenant_id == tenant_id, User.is_active.is_(True)))
    out = []
    for u in users:
        if not set(u.roles or []) & set(roles):
            continue
        q = select(ChannelIdentity).where(
            ChannelIdentity.tenant_id == tenant_id, ChannelIdentity.user_id == u.id
        )
        for ident in await session.scalars(q):
            if channel is None or ident.channel == channel:
                out.append((u, ident))
    return out


def dm(ident: ChannelIdentity) -> ConversationRef:
    return ConversationRef(
        channel=ident.channel,  # type: ignore[arg-type]
        conversation_id=ident.external_id,
        conversation_type="direct",
        user_id=ident.external_id,
    )


async def _thread(session: AsyncSession, tenant_id: str) -> str:
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


async def evaluate_new_orders(svc: Services, tenant_id: str, adapter: ChannelAdapter) -> list[str]:
    """评估最近新建、还没评估过的订单，推给计划员。返回评估了的订单。"""
    tenant = svc.registry.get(tenant_id)
    cfg = svc.planning.get(tenant_id)
    if tenant is None or cfg is None:
        return []
    tz = ZoneInfo(tenant.timezone)
    since = utcnow() - timedelta(hours=cfg.watch.lookback_hours)
    outbox = []
    done: list[str] = []
    async with svc.sessionmaker() as session, session.begin():
        rows = await session.scalars(
            select(ObjectState).where(
                ObjectState.tenant_id == tenant_id,
                ObjectState.object_type == "sales_order",
                ObjectState.data_mode == "real",
            )
        )
        fresh = [
            o
            for o in rows
            if o.state.get("status") in (None, "open")
            and (o.created_at if o.created_at.tzinfo else o.created_at.replace(tzinfo=ZoneInfo("UTC")))
            >= since
        ]
        if not fresh:
            return []
        to = await role_identities(session, tenant_id, cfg.watch.notify_roles, adapter.name)
        thread_id = await _thread(session, tenant_id)
        for o in sorted(fresh, key=lambda o: o.created_seq):
            trig = Trigger(
                tenant_id=tenant_id,
                source="schedule",
                external_id=f"order_eval:{o.object_id}",
                thread_id=thread_id,
                sender_ref="system",
                kind="schedule",
                route="order_eval",
                status="pending",
                payload={"order": o.object_id},
                created_at=utcnow(),
            )
            try:
                async with session.begin_nested():
                    session.add(trig)
            except IntegrityError:
                continue  # 评估过了
            try:
                e = await evaluate(
                    session,
                    tenant=tenant_id,
                    cfg=cfg,
                    ocfg=svc.orders[tenant_id],
                    tz=tz,
                    now=utcnow(),
                    order_id=o.object_id,
                )
            except PlanningError as err:
                trig.status, trig.result = "rejected", {"error": str(err)}
                continue
            trig.status = "done"
            trig.result = {"ok": e["ok"], "impacts": len(e["impacts"]), "to": [u.login for u, _ in to]}
            card = evaluation_card(e, mode=o.data_mode)
            outbox += [(dm(i), card) for _, i in to]
            done.append(o.object_id)
    for ref, card in outbox:  # 提交之后再发
        try:
            await adapter.send_card(ref, card, card_id="", short_code="")
        except ChannelError as err:
            log.warning("交期评估推送失败：%s", err)
    return done


async def order_watch_loop(svc: Services, tenant_id: str, adapter: ChannelAdapter) -> None:
    cfg = svc.planning.get(tenant_id)
    if cfg is None:
        return
    while True:
        try:
            done = await evaluate_new_orders(svc, tenant_id, adapter)
            if done:
                log.info("%s 新订单交期评估：%s", tenant_id, done)
        except Exception:  # 一次失败不影响下一轮
            log.exception("%s 新订单交期评估失败", tenant_id)
        await asyncio.sleep(cfg.watch.poll_minutes * 60)


async def send_texts(adapter_for, tenant_id: str, items: list[tuple[ConversationRef, str]]) -> list[str]:
    errors = []
    for ref, text in items:
        adapter = adapter_for(tenant_id, ref.channel)
        if adapter is None:
            continue
        try:
            await adapter.send_text(ref, text)
        except ChannelError as e:
            errors.append(str(e)[:200])
    return errors
