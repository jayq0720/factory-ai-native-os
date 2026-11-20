"""信道相关接口：IM 账号绑定、发卡片、查 Trigger、飞书/企业微信回调，以及开发用的模拟信道。"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from fos.api.deps import SessionDep, require
from fos.channels import feishu, wecom
from fos.channels.base import ChannelAdapter, ChannelError
from fos.channels.cards import Card
from fos.channels.dingtalk import DingTalkAdapter, DingTalkApp
from fos.channels.types import (
    Attachment,
    CardAction,
    ChannelName,
    ConversationRef,
    ConversationType,
    InboundMessage,
    MessageKind,
)
from fos.db.models import CardRecord, ChannelIdentity, Trigger, User
from fos.governance.permissions import is_valid_permission
from fos.governance.principal import Principal
from fos.triggers.cards import LedgerAppendAction, propose_card, send_card_record
from fos.triggers.intake import deliver, thread_for

router = APIRouter(prefix="/v1")

IdentityAdmin = Annotated[Principal, Depends(require("platform.identity:write"))]
CardWriter = Annotated[Principal, Depends(require("platform.card:write"))]
CardReader = Annotated[Principal, Depends(require("platform.card:read"))]
TriggerReader = Annotated[Principal, Depends(require("platform.trigger:read"))]
ChannelTester = Annotated[Principal, Depends(require("platform.channel:write"))]


def _mock_for(request: Request, tenant_id: str):
    from fos.channels.mock import MockChannel

    channels: dict[str, MockChannel] = request.app.state.mock_channels
    if tenant_id not in channels:
        channels[tenant_id] = MockChannel()
    return channels[tenant_id]


def adapter_for(request: Request, tenant_id: str, channel: str) -> ChannelAdapter:
    st = request.app.state
    if not st.intake.channel_enabled(tenant_id, channel):
        raise HTTPException(422, detail=f"没有启用信道 {channel}")
    if channel == "mock":
        return _mock_for(request, tenant_id)
    cache: dict[tuple[str, str], ChannelAdapter] = st.adapters
    key = (tenant_id, channel)
    if key in cache:
        return cache[key]
    if channel == "dingtalk":
        app = DingTalkApp.from_env(tenant_id)
        if app is None:
            raise HTTPException(503, detail="钉钉应用凭据没有配置")
        cache[key] = DingTalkAdapter(app)
        return cache[key]
    if channel == "feishu":
        app = feishu.FeishuApp.from_env(tenant_id)
        if app is None:
            raise HTTPException(503, detail="飞书应用凭据没有配置")
        cache[key] = feishu.FeishuAdapter(app)
        return cache[key]
    if channel == "wecom":
        app = wecom.WeComApp.from_env(tenant_id)
        if app is None:
            raise HTTPException(503, detail="企业微信应用凭据没有配置")
        cache[key] = wecom.WeComAdapter(app)
        return cache[key]
    raise HTTPException(501, detail=f"{channel} 适配器还没有实现")


def lookup_for(request: Request):
    """给审批、待办通知用：能用的信道返回适配器，没配置的返回 None（只进收件箱，不推送）。"""

    def _lookup(tenant_id: str, channel: str) -> ChannelAdapter | None:
        try:
            return adapter_for(request, tenant_id, channel)
        except HTTPException:
            return None

    return _lookup


# ---------------- IM 账号绑定 ----------------


class IdentityIn(BaseModel):
    channel: ChannelName
    external_id: str = Field(min_length=1, max_length=128)
    login: str


@router.put("/channels/identities")
async def bind_identity(
    body: IdentityIn, request: Request, session: SessionDep, principal: IdentityAdmin
) -> dict[str, Any]:
    if body.channel == "mock" and request.app.state.settings.env == "prod":
        raise HTTPException(422, detail="生产环境不允许绑定 mock 信道")
    user = (
        await session.scalars(
            select(User).where(User.tenant_id == principal.tenant_id, User.login == body.login)
        )
    ).one_or_none()
    if user is None:
        raise HTTPException(404, detail="用户不存在")
    key = (principal.tenant_id, body.channel, body.external_id)
    ident = await session.get(ChannelIdentity, key)
    if ident is None:
        session.add(ChannelIdentity(tenant_id=key[0], channel=key[1], external_id=key[2], user_id=user.id))
    else:
        ident.user_id = user.id
    await session.flush()
    return {"channel": body.channel, "external_id": body.external_id, "user_id": user.id, "login": user.login}


@router.delete("/channels/identities/{channel}/{external_id}", status_code=204)
async def unbind_identity(
    channel: str, external_id: str, session: SessionDep, principal: IdentityAdmin
) -> None:
    ident = await session.get(ChannelIdentity, (principal.tenant_id, channel, external_id))
    if ident is None:
        raise HTTPException(404, detail="没有这条绑定")
    await session.delete(ident)


# ---------------- 卡片 ----------------


class CardIn(BaseModel):
    to: ConversationRef
    card: Card
    action: LedgerAppendAction | None = None
    required_permission: str | None = None
    allowed_login: str | None = None  # 只允许这个人确认
    ttl_hours: int = Field(default=72, ge=1, le=24 * 14)


def _card_out(c: CardRecord) -> dict[str, Any]:
    return {
        "id": c.id,
        "short_code": c.short_code,
        "thread_id": c.thread_id,
        "channel": c.channel,
        "status": c.status,
        "result": c.result,
        "acted_by": c.acted_by,
        "acted_at": c.acted_at.isoformat() if c.acted_at else None,
        "expires_at": c.expires_at.isoformat(),
        "card": c.card,
    }


@router.post("/cards", status_code=201)
async def create_card(
    body: CardIn, request: Request, session: SessionDep, principal: CardWriter
) -> dict[str, Any]:
    """发一张需要人确认的卡片。确认后的动作以点击人的权限执行，发卡片的人不需要写权限。"""
    if body.required_permission and (
        not is_valid_permission(body.required_permission) or "*" in body.required_permission
    ):
        raise HTTPException(422, detail="required_permission 必须是不带通配符的合法权限串")
    allowed = None
    if body.allowed_login:
        u = (
            await session.scalars(
                select(User).where(User.tenant_id == principal.tenant_id, User.login == body.allowed_login)
            )
        ).one_or_none()
        if u is None:
            raise HTTPException(404, detail="allowed_login 用户不存在")
        allowed = u.id
    req_perm = body.required_permission
    if body.action is not None:
        onto = request.app.state.ontologies[principal.tenant_id]
        evdef = onto.events.get(body.action.request.type)
        if evdef is None:
            raise HTTPException(422, detail=f"未知事件类型：{body.action.request.type}")
        req_perm = req_perm or evdef.permission
    adapter = adapter_for(request, principal.tenant_id, body.to.channel)
    thread_id = await thread_for(session, principal.tenant_id, body.to)
    try:
        rec = await propose_card(
            session,
            tenant_id=principal.tenant_id,
            to=body.to,
            thread_id=thread_id,
            card=body.card,
            created_by=principal.subject_id,
            action=body.action,
            required_permission=req_perm,
            allowed_user_id=allowed,
            ttl=timedelta(hours=body.ttl_hours),
        )
    except ValueError as e:
        raise HTTPException(422, detail=str(e)) from e
    await session.commit()
    err = await send_card_record(adapter, rec)
    if err:
        rec.status, rec.result = "failed", {"error": err}
        await session.commit()
    return _card_out(rec)


@router.get("/cards/{card_id}")
async def get_card(card_id: str, session: SessionDep, principal: CardReader) -> dict[str, Any]:
    c = await session.get(CardRecord, card_id)
    if c is None or c.tenant_id != principal.tenant_id:
        raise HTTPException(404, detail="卡片不存在")
    return _card_out(c)


# ---------------- Trigger ----------------


@router.get("/triggers")
async def list_triggers(
    session: SessionDep,
    principal: TriggerReader,
    thread_id: str | None = None,
    route: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    q = select(Trigger).where(Trigger.tenant_id == principal.tenant_id)
    if thread_id:
        q = q.where(Trigger.thread_id == thread_id)
    if route:
        q = q.where(Trigger.route == route)
    rows = (await session.scalars(q.order_by(Trigger.created_at.desc()).limit(min(max(limit, 1), 200)))).all()
    return {
        "items": [
            {
                "id": t.id,
                "source": t.source,
                "thread_id": t.thread_id,
                "user_id": t.user_id,
                "kind": t.kind,
                "route": t.route,
                "status": t.status,
                "text": t.text,
                "result": t.result,
                "created_at": t.created_at.isoformat(),
            }
            for t in rows
        ]
    }


# ---------------- 模拟信道（非 prod） ----------------


class MockMessageIn(BaseModel):
    external_msg_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(default="mock-group", min_length=1, max_length=128)
    conversation_type: ConversationType = "group"
    sender_id: str = Field(min_length=1, max_length=128)
    sender_name: str = ""
    mentioned: bool = True
    kind: MessageKind = "text"
    text: str = Field(default="", max_length=4000)
    attachments: list[Attachment] = Field(default_factory=list)
    action: CardAction | None = None
    files: dict[str, str] = Field(default_factory=dict)  # 附件 ref → base64 内容


@router.post("/channels/mock/messages")
async def mock_message(
    body: MockMessageIn, request: Request, session: SessionDep, principal: ChannelTester
) -> dict[str, Any]:
    """模拟一条 IM 消息进入系统，走与真实信道完全相同的 Trigger 入口。仅限非 prod 环境。"""
    st = request.app.state
    if st.settings.env == "prod":
        raise HTTPException(404)
    mock = _mock_for(request, principal.tenant_id)
    max_bytes = st.settings.max_upload_mb * 1024 * 1024
    for ref, b64 in body.files.items():
        if len(b64) > (max_bytes * 4 // 3) + 16:
            raise HTTPException(413, detail=f"附件 {ref} 超过大小上限")
        try:
            raw = base64.b64decode(b64, validate=True)
        except binascii.Error as e:
            raise HTTPException(422, detail=f"附件 {ref} 不是合法的 base64") from e
        if len(raw) > max_bytes:
            raise HTTPException(413, detail=f"附件 {ref} 超过大小上限")
        mock.files[ref] = raw
    msg = InboundMessage(
        channel="mock",
        tenant_id=principal.tenant_id,
        **body.model_dump(exclude={"files"}),
    )
    out = await st.intake.handle(session, mock, msg)
    await session.commit()
    send_errors = await deliver(mock, out)
    t = out.trigger
    run = None
    if t is not None and not out.duplicate and t.route == "router" and t.status == "pending":
        ro = await st.services.runtime.process(st.sessionmaker, mock, t.id)
        out.replies.extend({"type": "text", "text": r} for r in ro.replies)
        out.replies.extend({"type": "card", "card_id": c.id, "short_code": c.short_code} for c in ro.cards)
        run = {"decision": ro.decision, "run_id": ro.run.id if ro.run else None}
        async with st.sessionmaker() as s2:
            t = await s2.get(Trigger, t.id)
    return {
        "ignored": t is None,
        "duplicate": out.duplicate,
        "trigger_id": t.id if t else None,
        "thread_id": t.thread_id if t else None,
        "route": t.route if t else None,
        "status": t.status if t else None,
        "result": t.result if t else None,
        "replies": out.replies,
        "send_errors": send_errors,
        "agent": run,
    }


@router.get("/channels/mock/outbox")
async def mock_outbox(request: Request, principal: ChannelTester) -> dict[str, Any]:
    if request.app.state.settings.env == "prod":
        raise HTTPException(404)
    return {"items": _mock_for(request, principal.tenant_id).outbox[-100:]}


# ---------------- 飞书 / 企业微信回调入口 ----------------


async def _dispatch_inbound(
    request: Request, session: SessionDep, adapter: ChannelAdapter, msg: InboundMessage
) -> None:
    st = request.app.state
    out = await st.intake.handle(session, adapter, msg)
    await session.commit()
    await deliver(adapter, out)
    t = out.trigger
    if t is not None and not out.duplicate and t.route == "router" and t.status == "pending":
        await st.services.runtime.process(st.sessionmaker, adapter, t.id)


@router.post("/channels/feishu/{tenant_id}/webhook")
async def feishu_webhook(tenant_id: str, request: Request, session: SessionDep) -> dict[str, Any]:
    """飞书事件订阅与互动卡片回调入口（含 URL 校验、SHA-256 签名与 AES-256-CBC 解密）。"""
    if not request.app.state.intake.channel_enabled(tenant_id, "feishu"):
        raise HTTPException(404, detail="租户未启用飞书信道")
    adapter = adapter_for(request, tenant_id, "feishu")
    assert isinstance(adapter, feishu.FeishuAdapter)
    app = adapter.app
    if not (app.encrypt_key or app.verification_token):
        raise HTTPException(503, detail="飞书回调校验密钥未配置")

    raw_body = await request.body()
    sig = request.headers.get("x-lark-signature", "")
    ts = request.headers.get("x-lark-request-timestamp", "")
    nonce = request.headers.get("x-lark-request-nonce", "")
    if app.encrypt_key and sig and not feishu.verify_signature(app.encrypt_key, ts, nonce, raw_body, sig):
        raise HTTPException(403, detail="飞书回调签名校验失败")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(400, detail="飞书回调不是合法 JSON") from e
    if not isinstance(payload, dict):
        raise HTTPException(400, detail="飞书回调负载必须为 JSON 对象")

    if "encrypt" in payload:
        if not app.encrypt_key:
            raise HTTPException(503, detail="收到飞书加密事件，但未配置 ENCRYPT_KEY")
        try:
            data = feishu.decrypt_event(app.encrypt_key, str(payload["encrypt"]))
        except ChannelError as e:
            raise HTTPException(403, detail=str(e)) from e
    else:
        if app.encrypt_key and not sig:
            raise HTTPException(403, detail="已配置 ENCRYPT_KEY，拒绝未签名明文回调")
        data = payload

    if app.verification_token:
        got_token = str((data.get("header") or {}).get("token") or data.get("token") or "")
        if not hmac.compare_digest(got_token, app.verification_token):
            raise HTTPException(403, detail="飞书回调 verification_token 校验失败")

    if data.get("type") == "url_verification":
        return {"challenge": str(data.get("challenge") or "")}

    msg = feishu.parse_event(tenant_id, data)
    if msg is not None:
        await _dispatch_inbound(request, session, adapter, msg)
        if msg.kind == "card_action":
            return {"toast": {"type": "info", "content": "已收到操作"}}
    return {"ok": True}


def _wecom_app_and_adapter(request: Request, tenant_id: str) -> tuple[wecom.WeComApp, wecom.WeComAdapter]:
    if not request.app.state.intake.channel_enabled(tenant_id, "wecom"):
        raise HTTPException(404, detail="租户未启用企业微信信道")
    adapter = adapter_for(request, tenant_id, "wecom")
    assert isinstance(adapter, wecom.WeComAdapter)
    app = adapter.app
    if not (app.token and app.encoding_aes_key):
        raise HTTPException(503, detail="企业微信回调 TOKEN / ENCODING_AES_KEY 未配置")
    return app, adapter


@router.get("/channels/wecom/{tenant_id}/webhook")
async def wecom_verify(
    tenant_id: str,
    request: Request,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
) -> Response:
    """企业微信回调 URL 校验（GET）：验签并解密 echostr 原样返回。"""
    app, _ = _wecom_app_and_adapter(request, tenant_id)
    assert app.token and app.encoding_aes_key
    if not wecom.verify_signature(app.token, timestamp, nonce, echostr, msg_signature):
        raise HTTPException(403, detail="企业微信回调签名校验失败")
    try:
        plain = wecom.decrypt_message(app.encoding_aes_key, app.corp_id, echostr)
    except ChannelError as e:
        raise HTTPException(403, detail=str(e)) from e
    return Response(content=plain, media_type="text/plain")


@router.post("/channels/wecom/{tenant_id}/webhook")
async def wecom_webhook(
    tenant_id: str,
    request: Request,
    session: SessionDep,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
) -> Response:
    """企业微信回调消息与模板卡片事件入口（POST）：验签、解密 XML 并送入 Trigger 入口。"""
    app, adapter = _wecom_app_and_adapter(request, tenant_id)
    assert app.token and app.encoding_aes_key
    xml_body = (await request.body()).decode("utf-8", errors="replace")
    try:
        enc = wecom.extract_encrypt_xml(xml_body)
    except ChannelError as e:
        raise HTTPException(400, detail=str(e)) from e
    if not wecom.verify_signature(app.token, timestamp, nonce, enc, msg_signature):
        raise HTTPException(403, detail="企业微信回调签名校验失败")
    try:
        inner_xml = wecom.decrypt_message(app.encoding_aes_key, app.corp_id, enc)
        msg = wecom.parse_xml_message(tenant_id, inner_xml)
    except ChannelError as e:
        raise HTTPException(400, detail=str(e)) from e
    if msg is not None:
        await _dispatch_inbound(request, session, adapter, msg)
    return Response(content="success", media_type="text/plain")
