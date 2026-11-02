"""Skill 与运行记录查询。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from fos.api.deps import PrincipalDep, SessionDep, require
from fos.db.models import Run
from fos.governance.principal import Principal

router = APIRouter(prefix="/v1")
RunReader = Annotated[Principal, Depends(require("platform.run:read"))]


@router.get("/skills")
async def list_skills(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    skills = request.app.state.services.skills.get(principal.tenant_id, {})
    return {
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "role": s.role,
                "version": s.version,
                "description": s.description,
                "autonomy": s.autonomy,
                "tools": s.tools,
                "proposes": s.proposes,
                "source": s.source,
            }
            for s in skills.values()
        ]
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str, session: SessionDep, principal: RunReader) -> dict[str, Any]:
    r = await session.get(Run, run_id)
    if r is None or r.tenant_id != principal.tenant_id:
        raise HTTPException(404, detail="运行记录不存在")
    return {
        "id": r.id,
        "thread_id": r.thread_id,
        "trigger_id": r.trigger_id,
        "user_id": r.user_id,
        "skill": {"id": r.skill_id, "version": r.skill_version},
        "status": r.status,
        "task": r.task,
        "steps": r.steps,
        "result": r.result,
        "data_mode": r.data_mode,
        "tokens": {"prompt": r.prompt_tokens, "completion": r.completion_tokens},
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }
