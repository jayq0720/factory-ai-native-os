"""试点上线支持的接口（W13）。

- GET  /v1/import/template：按本体生成的主数据导入模板（xlsx）；
- POST /v1/import/preview：上传填好的模板，逐行试入账后全部回滚，返回每一行的结果；
- POST /v1/import/commit：没有任何错误才一次全部入账（以导入人身份 Ledger.append，证据 = 原件 + 行号）；
- GET  /v1/reports/daily?date=YYYY-MM-DD：生产日报（账本确定性统计，只含有权查看的内容）。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from fos.api.deps import PrincipalDep, SessionDep, require
from fos.db.models import utcnow
from fos.governance.principal import Principal
from fos.onboarding.importer import ImportFileError, build_template, run_import
from fos.reports.daily import daily_report, report_lines

router = APIRouter(prefix="/v1")
Importer = Annotated[Principal, Depends(require("platform.import:write"))]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/import/template")
async def import_template(request: Request, principal: Importer) -> Response:
    st = request.app.state
    tenant = st.registry.get(principal.tenant_id)
    data = build_template(st.imports[principal.tenant_id], st.ontologies[principal.tenant_id], tenant.name)
    return Response(
        data,
        media_type=XLSX,
        headers={"Content-Disposition": "attachment; filename*=UTF-8''master-data-template.xlsx"},
    )


async def _import(request: Request, session, principal: Principal, file: UploadFile, *, commit: bool):
    st = request.app.state
    limit = st.settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, detail=f"文件超过 {st.settings.max_upload_mb} MB")
    tenant = st.registry.get(principal.tenant_id)
    digest = st.blobs.put(principal.tenant_id, data) if commit else ""
    try:
        rep = await run_import(
            session,
            ledger=st.ledger,
            principal=principal,
            onto=st.ontologies[principal.tenant_id],
            cfg=st.imports[principal.tenant_id],
            tz=ZoneInfo(tenant.timezone),
            data=data,
            filename=file.filename or "import.xlsx",
            evidence_ref=st.blobs.uri(principal.tenant_id, digest) if commit else "preview",
            commit=commit,
        )
    except ImportFileError as e:
        raise HTTPException(422, detail=str(e)) from e
    return rep.out()


@router.post("/import/preview")
async def import_preview(
    request: Request, session: SessionDep, principal: Importer, file: UploadFile
) -> dict[str, Any]:
    return await _import(request, session, principal, file, commit=False)


@router.post("/import/commit")
async def import_commit(request: Request, session: SessionDep, principal: Importer, file: UploadFile):
    out = await _import(request, session, principal, file, commit=True)
    return JSONResponse(out, status_code=201 if out["committed"] else 422)


@router.get("/reports/daily")
async def get_daily(
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
    day: Annotated[date | None, Query(alias="date")] = None,
) -> dict[str, Any]:
    st = request.app.state
    tenant = st.registry.get(principal.tenant_id)
    tz = ZoneInfo(tenant.timezone)
    rep = await daily_report(
        session,
        onto=st.ontologies[principal.tenant_id],
        cfg=st.dailies[principal.tenant_id],
        trace=st.traces.get(principal.tenant_id),
        principal=principal,
        tz=tz,
        day=day or utcnow().astimezone(tz).date(),
    )
    rep["lines"] = report_lines(rep)
    return rep
