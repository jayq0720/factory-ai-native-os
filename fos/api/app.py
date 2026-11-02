"""FastAPI 入口：uvicorn fos.api.app:app"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from fos import __version__
from fos.api.deps import PrincipalDep, RegistryDep, SessionDep, require
from fos.api.ledger_routes import router as ledger_router
from fos.config import Settings, get_settings
from fos.db.session import make_engine, make_sessionmaker
from fos.gateway.llm import MODEL_ALIASES, ChatMessage, LLMClient, LLMError
from fos.governance.principal import Principal
from fos.ledger import Ledger
from fos.ontology.loader import load_ontology
from fos.tenancy import TenantRegistry


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
        engine = make_engine(settings.database_url)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        registry = TenantRegistry.load(settings.tenants_dir)
        app.state.registry = registry
        # 启动时加载并校验每个租户的本体；任何一个不合法就拒绝启动
        app.state.ontologies = {
            tid: load_ontology(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
        }
        app.state.ledger = Ledger(lambda tid: app.state.ontologies[tid])
        app.state.llm = LLMClient.from_settings(settings)
        try:
            yield
        finally:
            await app.state.llm.aclose()
            await engine.dispose()

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
        return {"status": "ready", "tenants": len(registry.ids()), "llm_mode": settings.llm_mode.value}

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

    app.include_router(ledger_router)
    return app


app = create_app()
