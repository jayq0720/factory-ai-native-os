"""标准 MCP（Model Context Protocol）JSON-RPC 2.0 服务端点（POST /v1/mcp）。

遵循 MCP 2024-11-05 协议规范（Streamable HTTP），向外部 Agent 暴露平台已注册工具：
- 复用统一认证（Bearer API Key 或 Web HttpOnly 会话）与 RBAC 权限边界；
- 严格只读/提议模式，调用方无法越权，凭证永不进入工具上下文。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from fos import __version__
from fos.api.deps import PrincipalDep, SessionDep
from fos.skills.schema import SkillDef
from fos.tools import builtin as _builtin_tools  # noqa: F401  确保内置工具已注册
from fos.tools.registry import REGISTRY, ToolContext, ToolError

router = APIRouter(prefix="/v1", tags=["mcp"])

MCP_PROTOCOL_VERSION = "2024-11-05"


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] | None = None


def _ok(req_id: str | int | None, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: str | int | None, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


@router.post("/mcp")
async def mcp_endpoint(
    body: JsonRpcRequest,
    request: Request,
    session: SessionDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    """MCP JSON-RPC 2.0 入口：支持 initialize、tools/list、tools/call。"""
    method = body.method
    params = body.params or {}

    if method == "initialize":
        return _ok(
            body.id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "factory-ai-native-os", "version": __version__},
            },
        )

    if method in ("notifications/initialized", "ping"):
        return _ok(body.id, {})

    if method == "tools/list":
        tools = [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.args.model_json_schema(),
            }
            for t in sorted(REGISTRY.values(), key=lambda x: x.name)
        ]
        return _ok(body.id, {"tools": tools})

    if method == "tools/call":
        name = params.get("name")
        raw_args = params.get("arguments") or {}
        if not isinstance(name, str) or name not in REGISTRY:
            return _err(body.id, -32602, f"未知工具：{name!r}")
        if not isinstance(raw_args, dict):
            return _err(body.id, -32602, "arguments 必须是 JSON 对象")

        onto = request.app.state.ontologies[principal.tenant_id]
        mcp_skill = SkillDef(
            id="mcp_external",
            name="MCP 外部调用",
            role="MCP 客户端",
            version=1,
            description="经 /v1/mcp 认证主体的权限边界",
            tools=[name],
            permissions=sorted(principal.permissions),
            instructions="",
        )
        extras: dict[str, Any] = {}
        svc = request.app.state.services
        if principal.tenant_id in svc.orders:
            extras["orders"] = svc.orders[principal.tenant_id]
        if principal.tenant_id in svc.planning:
            extras["planning"] = svc.planning[principal.tenant_id]
        if principal.tenant_id in svc.supply:
            extras["supply"] = svc.supply[principal.tenant_id]
        ctx = ToolContext(
            session=session,
            user=principal,
            skill=mcp_skill,
            ontology=onto,
            extras=extras,
        )
        try:
            res = await REGISTRY[name].call(ctx, raw_args)
        except ToolError as e:
            return _ok(body.id, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        return _ok(
            body.id,
            {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, default=str)}],
                "isError": False,
            },
        )

    return _err(body.id, -32601, f"不支持的 MCP 方法：{method}")
