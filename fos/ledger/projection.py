"""投影：把事件机械地应用到对象当前状态和引用图上。

追加时在同一事务里调用；重建时按 seq 顺序重放全部事件调用同一个函数，
所以“当前状态”永远等于“事件的折叠”，不会出现两套逻辑。
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import Event, ObjectLink, ObjectState
from fos.ontology import Ontology


async def apply_event(session: AsyncSession, onto: Ontology, ev: Event, op: str) -> ObjectState:
    key = (ev.tenant_id, ev.object_type, ev.object_id)
    obj = await session.get(ObjectState, key)
    if op == "create":
        if obj is not None:
            raise RuntimeError(f"投影冲突：{key} 已存在")
        obj = ObjectState(
            tenant_id=ev.tenant_id,
            object_type=ev.object_type,
            object_id=ev.object_id,
            state={k: v for k, v in ev.payload.items() if v is not None},
            data_mode=ev.data_mode,
            version=1,
            created_seq=ev.seq,
            updated_seq=ev.seq,
            created_at=ev.occurred_at,
            updated_at=ev.occurred_at,
        )
        session.add(obj)
    else:
        if obj is None:
            raise RuntimeError(f"投影缺失：{key}")
        state = dict(obj.state)
        for k, v in ev.payload.items():
            if v is None:
                state.pop(k, None)  # 显式置空 = 清除该属性
            else:
                state[k] = v
        obj.state = state
        obj.version += 1
        obj.updated_seq = ev.seq
        obj.updated_at = ev.occurred_at
    await _sync_links(session, onto, obj)
    return obj


async def _sync_links(session: AsyncSession, onto: Ontology, obj: ObjectState) -> None:
    await session.execute(
        delete(ObjectLink).where(
            ObjectLink.tenant_id == obj.tenant_id,
            ObjectLink.from_type == obj.object_type,
            ObjectLink.from_id == obj.object_id,
        )
    )
    for rel, prop in onto.ref_props(obj.object_type).items():
        val = obj.state.get(rel)
        if val is None:
            continue
        for to_id in val if prop.many else [val]:
            session.add(
                ObjectLink(
                    tenant_id=obj.tenant_id,
                    from_type=obj.object_type,
                    from_id=obj.object_id,
                    rel=rel,
                    to_type=prop.target,
                    to_id=to_id,
                )
            )


async def rebuild(session: AsyncSession, onto: Ontology, tenant_id: str) -> int:
    """清空该租户的投影并按 seq 重放全部事件。返回重放的事件数。"""
    await session.execute(delete(ObjectLink).where(ObjectLink.tenant_id == tenant_id))
    await session.execute(delete(ObjectState).where(ObjectState.tenant_id == tenant_id))
    await session.flush()
    n = 0
    seen: set[tuple[str, str]] = set()
    after = 0
    while True:
        batch = (
            await session.scalars(
                select(Event)
                .where(Event.tenant_id == tenant_id, Event.seq > after)
                .order_by(Event.seq)
                .limit(500)
            )
        ).all()
        if not batch:
            break
        for ev in batch:
            key = (ev.object_type, ev.object_id)
            await apply_event(session, onto, ev, "update" if key in seen else "create")
            await session.flush()
            seen.add(key)
            n += 1
        after = batch[-1].seq
    return n
