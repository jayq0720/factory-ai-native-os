"""链式召回：沿引用图双向展开（方案 3.3 批次追溯）。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import ObjectLink, ObjectState


async def trace(
    session: AsyncSession, tenant: str, object_type: str, object_id: str, *, depth: int = 4, limit: int = 200
) -> dict[str, Any]:
    start = (object_type, object_id)
    seen: dict[tuple[str, str], int] = {start: 0}
    edges: set[tuple[str, str, str, str, str]] = set()
    frontier = [start]
    for d in range(1, depth + 1):
        if not frontier or len(seen) >= limit:
            break
        conds = [
            or_(
                and_(ObjectLink.from_type == t, ObjectLink.from_id == i),
                and_(ObjectLink.to_type == t, ObjectLink.to_id == i),
            )
            for t, i in frontier
        ]
        rows = (
            await session.scalars(select(ObjectLink).where(ObjectLink.tenant_id == tenant, or_(*conds)))
        ).all()
        nxt: list[tuple[str, str]] = []
        for link in rows:
            edges.add((link.from_type, link.from_id, link.rel, link.to_type, link.to_id))
            for node in ((link.from_type, link.from_id), (link.to_type, link.to_id)):
                if node not in seen and len(seen) < limit:
                    seen[node] = d
                    nxt.append(node)
        frontier = nxt

    nodes = []
    for (t, i), d in sorted(seen.items(), key=lambda kv: (kv[1], kv[0])):
        obj = await session.get(ObjectState, (tenant, t, i))
        nodes.append(
            {
                "object_type": t,
                "object_id": i,
                "distance": d,
                "state": obj.state if obj else None,
                "data_mode": obj.data_mode if obj else None,
            }
        )
    keep = {(n["object_type"], n["object_id"]) for n in nodes}
    return {
        "root": {"object_type": object_type, "object_id": object_id},
        "nodes": nodes,
        "edges": [
            {"from_type": a, "from_id": b, "rel": r, "to_type": c, "to_id": e}
            for a, b, r, c, e in sorted(edges)
            if (a, b) in keep and (c, e) in keep
        ],
    }
