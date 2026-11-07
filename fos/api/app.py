"""FastAPI 入口：uvicorn fos.api.app:app"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text

from fos import __version__
from fos.api.agent_routes import router as agent_router
from fos.api.channel_routes import router as channel_router
from fos.api.deps import PrincipalDep, RegistryDep, SessionDep, require
from fos.api.document_routes import router as document_router
from fos.api.eng_routes import router as eng_router
from fos.api.inbox_routes import router as inbox_router
from fos.api.ledger_routes import router as ledger_router
from fos.api.production_routes import router as production_router
from fos.channels.mock import MockChannel
from fos.config import Settings, get_settings
from fos.gateway.llm import MODEL_ALIASES, ChatMessage, LLMClient, LLMError
from fos.governance.principal import Principal
from fos.services import Services


class MeOut(BaseModel):
    tenant_id: str
    tenant_name: str
    subject_id: str
    display_name: str
    roles: list[str]
    permissions: list[str]


class ChatIn(BaseModel):
    messages: list[ChatMessage]
    model: str = "fos-chat"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = Services.build(settings)
        st = app.state
        st.services = svc
        st.settings, st.engine, st.sessionmaker = settings, svc.engine, svc.sessionmaker
        st.registry, st.ontologies, st.ledger = svc.registry, svc.ontologies, svc.ledger
        st.policies, st.approvals = svc.policies, svc.approvals
        st.llm, st.ocr, st.blobs, st.intake = svc.llm, svc.ocr, svc.blobs, svc.intake
        st.extractor, st.jobs, st.traces = svc.extractor, svc.jobs, svc.traces
        await svc.jobs.recover()  # 上次没跑完的解析任务接着跑
        st.mock_channel = MockChannel()  # 模拟信道（非 prod 环境），见 channel_routes
        st.adapters = {}  # 按租户缓存的真实信道适配器（发卡片用）
        try:
            yield
        finally:
            for a in st.adapters.values():
                await a.aclose()
            await svc.aclose()

    app = FastAPI(title="Factory AI Native OS", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(session: SessionDep, registry: RegistryDep) -> dict[str, Any]:
        await session.execute(text("SELECT 1"))
        return {
            "status": "ready",
            "tenants": len(registry.ids()),
            "llm_mode": settings.llm_mode.value,
            "ocr_mode": settings.ocr_mode.value,
            "llm_extraction": settings.extract_with_llm and settings.llm_mode.value == "litellm",
        }

    @app.get("/v1/me", response_model=MeOut)
    async def me(principal: PrincipalDep, registry: RegistryDep) -> MeOut:
        tenant = registry.get(principal.tenant_id)
        assert tenant is not None
        return MeOut(
            tenant_id=principal.tenant_id,
            tenant_name=tenant.name,
            subject_id=principal.subject_id,
            display_name=principal.display_name,
            roles=list(principal.roles),
            permissions=sorted(principal.permissions),
        )

    @app.post("/v1/llm/chat")
    async def llm_chat(
        body: ChatIn,
        request: Request,
        principal: Annotated[Principal, Depends(require("platform.llm:write"))],
    ) -> dict[str, Any]:
        """调试用：经网关调用模型。正式业务由 Worker 调用，不直接暴露给前端。"""
        if body.model not in MODEL_ALIASES:
            return JSONResponse({"detail": f"未知模型别名 {body.model!r}"}, status_code=400)
        llm: LLMClient = request.app.state.llm
        try:
            result = await llm.chat(body.messages, model=body.model, tenant_id=principal.tenant_id)
        except LLMError as e:
            return JSONResponse({"detail": str(e)}, status_code=502)
        return result.model_dump()

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/app/")

    # 文档解析网页（静态文件，无构建步骤）；所有数据仍走 /v1 接口与 API Key
    static = Path(__file__).parent.parent / "web" / "static"
    app.mount("/app", StaticFiles(directory=static, html=True), name="web")

    app.include_router(ledger_router)
    app.include_router(document_router)
    app.include_router(channel_router)
    app.include_router(agent_router)
    app.include_router(inbox_router)
    app.include_router(eng_router)
    app.include_router(production_router)
    return app


app = create_app()
