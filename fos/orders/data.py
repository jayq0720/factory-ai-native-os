"""读投影的小工具：按 id 取对象、按引用反查（谁引用了我）。"""

from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectLink, ObjectState


async def get(
    session: AsyncSession, tenant: str, object_type: str, object_id: str | None
) -> ObjectState | None:
    if not object_id:
        return None
    return await session.get(ObjectState, (tenant, object_type, object_id))


async def referrers(
    session: AsyncSession, tenant: str, from_type: str, rel: str, to_type: str, to_ids: list[str]
) -> list[ObjectState]:
    """引用了 to_ids 中任一对象的 from_type 对象（去重，按创建顺序）。"""
    if not to_ids:
        return []
    q = (
        select(ObjectState)
        .join(
            ObjectLink,
            and_(
                ObjectLink.tenant_id == ObjectState.tenant_id,
                ObjectLink.from_type == ObjectState.object_type,
                ObjectLink.from_id == ObjectState.object_id,
            ),
        )
        .where(
            ObjectLink.tenant_id == tenant,
            ObjectLink.from_type == from_type,
            ObjectLink.rel == rel,
            ObjectLink.to_type == to_type,
            ObjectLink.to_id.in_(to_ids),
        )
    )
    # 不用 SQL DISTINCT：PostgreSQL 的 json 列不能比较相等；一个对象引用多个 to_ids 时在这里去重
    seen: dict[str, ObjectState] = {}
    for o in (await session.scalars(q)).all():
        seen.setdefault(o.object_id, o)
    return sorted(seen.values(), key=lambda o: o.created_seq)


async def find_by(
    session: AsyncSession, tenant: str, object_type: str, key: str, value: str
) -> list[ObjectState]:
    """按对象 id 或某个属性等值查找（单厂规模，扫一类对象）。"""
    hit = await get(session, tenant, object_type, value)
    if hit is not None:
        return [hit]
    rows = await session.scalars(
        select(ObjectState).where(ObjectState.tenant_id == tenant, ObjectState.object_type == object_type)
    )
    return [o for o in rows if str(o.state.get(key) or "") == value]


def num(v) -> float:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0.0
