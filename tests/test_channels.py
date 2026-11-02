"""信道层：钉钉解析与发送、卡片 DSL、Trigger 入口、卡片确认（L2）。"""

from __future__ import annotations

import base64
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from fos.channels.cards import Card, CardButton, to_markdown
from fos.channels.dingtalk import (
    TOPIC_BOT_MESSAGE,
    TOPIC_CARD_CALLBACK,
    DingTalkAdapter,
    DingTalkApp,
    decode_frame,
    parse_bot_message,
    parse_card_callback,
)
from fos.channels.types import Attachment, ConversationRef
from tests import samples

# ---------------- 钉钉：解析 ----------------

BOT_TEXT = {
    "conversationId": "cidGROUP==",
    "conversationType": "2",
    "msgId": "msgA",
    "senderNick": "张班长",
    "senderStaffId": "staff001",
    "senderId": "$:LWCP_v1:$xyz",
    "isInAtList": True,
    "createAt": 1762070400000,
    "msgtype": "text",
    "text": {"content": " 3 号窑温度不对 "},
    "robotCode": "dingabc",
}


def test_parse_bot_text_group():
    m = parse_bot_message("luneng", BOT_TEXT)
    assert m.conversation_type == "group" and m.mentioned
    assert m.sender_id == "staff001"  # 优先企业内 userid
    assert m.text == "3 号窑温度不对" and m.kind == "text"
    assert m.reply_to().user_id is None


def test_parse_bot_picture_file_rich_direct():
    pic = {**BOT_TEXT, "msgtype": "picture", "conversationType": "1", "content": {"downloadCode": "dc1"}}
    m = parse_bot_message("luneng", pic)
    assert m.kind == "image" and m.attachments[0].ref == "dc1"
    assert m.conversation_type == "direct" and m.mentioned and m.reply_to().user_id == "staff001"

    f = {**BOT_TEXT, "msgtype": "file", "content": {"downloadCode": "dc2", "fileName": "技术协议.pdf"}}
    assert parse_bot_message("luneng", f).attachments[0].filename == "技术协议.pdf"

    rich = {
        **BOT_TEXT,
        "msgtype": "richText",
        "content": {"richText": [{"text": "送货单"}, {"downloadCode": "dc3", "type": "picture"}]},
    }
    m = parse_bot_message("luneng", rich)
    assert m.kind == "rich" and m.text == "送货单" and m.attachments[0].ref == "dc3"


def test_parse_card_callback():
    data = {
        "outTrackId": "card-1",
        "userId": "staff001",
        "spaceType": "IM_GROUP",
        "spaceId": "cidGROUP==",
        "content": json.dumps({"cardPrivateData": {"actionIds": ["x"], "params": {"action": "confirm"}}}),
    }
    m = parse_card_callback("luneng", data, "mid-9")
    assert m is not None and m.kind == "card_action"
    assert m.action is not None and m.action.card_id == "card-1" and m.action.action == "confirm"
    assert m.external_msg_id == "card:mid-9"
    assert parse_card_callback("luneng", {"outTrackId": "c"}, "m") is None


def test_decode_frame_ping_and_callback():
    ping = {"type": "SYSTEM", "headers": {"topic": "ping", "messageId": "p1"}, "data": '{"opaque":"x"}'}
    reply, msg = decode_frame("luneng", ping)
    assert msg is None and json.loads(reply)["data"] == '{"opaque":"x"}'

    frame = {
        "type": "CALLBACK",
        "headers": {"topic": TOPIC_BOT_MESSAGE, "messageId": "m1"},
        "data": json.dumps(BOT_TEXT),
    }
    reply, msg = decode_frame("luneng", frame)
    assert json.loads(reply)["headers"]["messageId"] == "m1" and json.loads(reply)["code"] == 200
    assert msg is not None and msg.external_msg_id == "msgA"

    bad = {"type": "CALLBACK", "headers": {"topic": TOPIC_CARD_CALLBACK, "messageId": "m2"}, "data": "{}"}
    reply, msg = decode_frame("luneng", bad)
    assert reply and msg is None  # 解析不了也要 ack，避免重推


def test_app_from_env_keeps_secret_out_of_repr():
    env = {"FOS_DINGTALK_LUNENG_CLIENT_ID": "dingabc", "FOS_DINGTALK_LUNENG_CLIENT_SECRET": "s3cret"}
    app = DingTalkApp.from_env("luneng", env)
    assert app is not None and app.robot_code == "dingabc" and app.card_template_id is None
    assert "s3cret" not in repr(app)
    assert DingTalkApp.from_env("other", env) is None


# ---------------- 钉钉：发送（HTTP 打桩） ----------------


def _stub_dingtalk(calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, dict(req.headers), body))
        if req.url.path == "/v1.0/oauth2/accessToken":
            return httpx.Response(200, json={"accessToken": "tok", "expireIn": 7200})
        if req.url.path == "/v1.0/robot/messageFiles/download":
            return httpx.Response(200, json={"downloadUrl": "https://files.example/f1"})
        if req.url.host == "files.example":
            return httpx.Response(200, content=b"x" * 100)
        return httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_dingtalk_send_text_and_markdown_card():
    calls: list = []
    app = DingTalkApp("luneng", "dingabc", "secret", "robot1")
    a = DingTalkAdapter(app, http=_stub_dingtalk(calls))
    group = ConversationRef(channel="dingtalk", conversation_id="cidG", conversation_type="group")
    await a.send_text(group, "你好")
    await a.send_text(group, "再来")
    assert [c[1] for c in calls].count("/v1.0/oauth2/accessToken") == 1  # token 缓存
    _, path, headers, body = calls[1]
    assert path == "/v1.0/robot/groupMessages/send" and headers["x-acs-dingtalk-access-token"] == "tok"
    assert body["openConversationId"] == "cidG" and body["robotCode"] == "robot1"
    assert json.loads(body["msgParam"]) == {"content": "你好"}

    card = Card(title="确认入库", buttons=[CardButton(id="confirm", label="确认")], data_mode="mock")
    await a.send_card(group, card, card_id="c1", short_code="K7Q2M")
    body = calls[-1][3]
    assert body["msgKey"] == "sampleMarkdown"
    text = json.loads(body["msgParam"])["text"]
    assert "模拟数据" in text and "@我 确认 K7Q2M" in text

    direct = ConversationRef(
        channel="dingtalk", conversation_id="cidD", conversation_type="direct", user_id="u1"
    )
    await a.send_text(direct, "单聊")
    assert calls[-1][1] == "/v1.0/robot/oToMessages/batchSend" and calls[-1][3]["userIds"] == ["u1"]

    data = await a.fetch_attachment(Attachment(kind="image", ref="dc1"), max_bytes=1000)
    assert data == b"x" * 100
    with pytest.raises(Exception, match="过大"):
        await a.fetch_attachment(Attachment(kind="image", ref="dc1"), max_bytes=10)
    await a.aclose()


async def test_dingtalk_interactive_card_when_template_configured():
    calls: list = []
    app = DingTalkApp("luneng", "dingabc", "secret", "robot1", card_template_id="tpl.schema")
    a = DingTalkAdapter(app, http=_stub_dingtalk(calls))
    assert a.supports_card_callback
    to = ConversationRef(channel="dingtalk", conversation_id="cidG", conversation_type="group")
    card = Card(
        title="确认入库",
        buttons=[CardButton(id="confirm", label="确认"), CardButton(id="cancel", label="取消")],
    )
    await a.send_card(to, card, card_id="c1", short_code="K7Q2M")
    _, path, _, body = calls[-1]
    assert path == "/v1.0/card/instances/createAndDeliver"
    assert body["outTrackId"] == "c1" and body["callbackType"] == "STREAM"
    assert body["openSpaceId"] == "dtv1.card//IM_GROUP.cidG"
    assert [b["id"] for b in json.loads(body["cardData"]["cardParamMap"]["buttons"])] == ["confirm", "cancel"]
    await a.aclose()


# ---------------- 卡片 DSL ----------------


def test_card_validation():
    with pytest.raises(ValidationError):
        Card(title="x", buttons=[CardButton(id="a", label="A"), CardButton(id="a", label="B")])
    with pytest.raises(ValidationError):
        Card(title="x", buttons=[CardButton(id=f"b{c}", label="B") for c in "abcd"])
    with pytest.raises(ValidationError):
        CardButton(id="Confirm!", label="确认")
    c = Card(title="t", buttons=[CardButton(id="confirm", label="确认")])
    assert c.button("确认") is c.button("CONFIRM") is c.buttons[0]
    assert "模拟数据" not in to_markdown(c)


# ---------------- Trigger 入口（模拟信道，走完整 API） ----------------


@pytest.fixture
def h(keys):
    return lambda who: {"Authorization": f"Bearer {keys[who]}"}


@pytest.fixture
def send(client, h):
    n = {"i": 0}

    def _send(sender: str, expect: int = 200, **kw):
        n["i"] += 1
        body = {"external_msg_id": kw.pop("external_msg_id", f"m{n['i']}"), "sender_id": sender, **kw}
        r = client.post("/v1/channels/mock/messages", json=body, headers=h("admin"))
        assert r.status_code == expect, r.text
        return r.json()

    return _send


@pytest.fixture
def bind(client, h):
    def _bind(external_id: str, login: str):
        r = client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": external_id, "login": login},
            headers=h("admin"),
        )
        assert r.status_code == 200, r.text

    return _bind


def test_unbound_and_ignored(client, send, bind, h):
    r = send("stranger", text="你好")
    assert r["route"] == "unbound" and r["status"] == "rejected"
    assert "stranger" in r["replies"][0]["text"]

    bind("im-worker", "worker")
    r = send("im-worker", text="闲聊", mentioned=False)
    assert r["ignored"] and r["trigger_id"] is None  # 群聊没 @：不落库

    r1 = send("im-worker", text="昨天合格率多少", external_msg_id="dup-1")
    assert r1["route"] == "router" and r1["status"] == "pending"
    r2 = send("im-worker", text="昨天合格率多少", external_msg_id="dup-1")
    assert r2["duplicate"] and r2["trigger_id"] == r1["trigger_id"] and r2["replies"] == []

    dm = send("im-worker", text="单聊", conversation_type="direct", conversation_id="dm", mentioned=False)
    assert dm["route"] == "router" and dm["thread_id"] != r1["thread_id"]
    again = send("im-worker", text="同一个群")
    assert again["thread_id"] == r1["thread_id"]  # 同一会话同一 Thread

    items = client.get("/v1/triggers", params={"thread_id": r1["thread_id"]}, headers=h("admin")).json()[
        "items"
    ]
    assert {t["text"] for t in items} == {"你好", "昨天合格率多少", "同一个群"}  # 未绑定的也记录
    assert client.get("/v1/triggers", headers=h("worker")).status_code == 403


def test_photo_goes_to_document_parsing(client, send, bind, h):
    bind("im-worker", "worker")
    png = samples.png()
    att = {"kind": "image", "ref": "pic-1", "filename": "送货单.png"}
    files = {"pic-1": base64.b64encode(png).decode()}
    r = send("im-worker", kind="image", attachments=[att], files=files)
    assert r["route"] == "doc_intake" and r["status"] == "done"
    card = r["replies"][0]["card"]
    assert card["data_mode"] == "mock" and card["title"] == "已收到并识别"
    doc_id = r["result"]["documents"][0]["document_id"]
    d = client.get(f"/v1/documents/{doc_id}", headers=h("admin")).json()
    assert d["filename"] == "送货单.png" and d["notice"]

    r = send("im-worker", kind="image", attachments=[att], files=files)
    assert r["replies"][0]["card"]["title"] == "这份文件之前已收到"

    bind("im-gm", "gm")  # 总经理只有读权限
    r = send("im-gm", kind="image", attachments=[att], files=files)
    assert r["route"] == "doc_intake" and r["status"] == "rejected"

    r = send("im-worker", kind="file", attachments=[{"kind": "file", "ref": "nope"}])
    assert r["status"] == "failed" and "不存在" in r["replies"][0]["text"]


# ---------------- 卡片确认（L2） ----------------

GROUP = {"channel": "mock", "conversation_id": "mock-group", "conversation_type": "group"}


def _card_body(**kw):
    body = {
        "to": GROUP,
        "card": {
            "title": "新增物料：硅酸铝纤维棉",
            "fields": [{"label": "单位", "value": "kg"}],
            "buttons": [
                {"id": "confirm", "label": "确认", "style": "primary"},
                {"id": "cancel", "label": "取消"},
            ],
        },
        "action": {
            "kind": "ledger.append",
            "request": {
                "type": "warehouse.material_defined",
                "object_id": "M-001",
                "payload": {"name": "硅酸铝纤维棉", "category": "raw", "unit": "kg"},
            },
        },
    }
    body.update(kw)
    return body


def test_card_confirm_writes_ledger_once_with_clicker_identity(client, send, bind, h):
    bind("im-worker", "worker")
    bind("im-wh", "warehouse")
    r = client.post("/v1/cards", json=_card_body(), headers=h("admin"))
    assert r.status_code == 201, r.text
    card = r.json()
    code = card["short_code"]
    out = client.get("/v1/channels/mock/outbox", headers=h("admin")).json()["items"][-1]
    assert out["type"] == "card" and out["card_id"] == card["id"] and code in out["markdown"]

    # 工人没有物料写权限：不写入，卡片保持可确认
    r = send("im-worker", text=f"确认 {code}")
    assert r["route"] == "card_action" and r["status"] == "rejected"
    assert "warehouse.material:write" in r["replies"][0]["text"]
    assert client.get(f"/v1/cards/{card['id']}", headers=h("admin")).json()["status"] == "open"

    # 仓管用文字指令确认（群里没 @ 也认，因为是本会话卡片的指令）
    r = send("im-wh", text=f"确认 {code.lower()}", mentioned=False)
    assert r["status"] == "done" and r["thread_id"] == card["thread_id"]
    seq = r["result"]["seq"]
    c = client.get(f"/v1/cards/{card['id']}", headers=h("admin")).json()
    assert c["status"] == "done" and c["result"]["event_id"]

    obj = client.get("/v1/objects/material/M-001", headers=h("admin")).json()
    ev = obj["history"][-1]
    assert ev["seq"] == seq and ev["actor"]["name"] == "warehouse"  # 以点击人身份记账
    assert any(e["kind"] == "approval" and e["ref"] == f"card:{card['id']}" for e in ev["evidence"])

    # 再点一次（按钮回调）：不会重复写
    r = send("im-wh", kind="card_action", action={"card_id": card["id"], "action": "confirm"})
    assert r["result"]["outcome"] == "already"
    assert len(client.get("/v1/objects/material/M-001", headers=h("admin")).json()["history"]) == 1


def test_card_cancel_restrictions_expiry_and_isolation(client, send, bind, h, keys):
    bind("im-wh", "warehouse")
    bind("im-qc", "qc")
    body = _card_body()
    body["action"]["request"]["object_id"] = "M-002"
    card = client.post("/v1/cards", json=body, headers=h("admin")).json()
    r = send("im-wh", kind="card_action", action={"card_id": card["id"], "action": "cancel"})
    assert r["result"]["outcome"] == "cancelled"
    assert client.get("/v1/objects/material/M-002", headers=h("admin")).status_code == 404

    # 指定只有仓管能确认
    only = client.post("/v1/cards", json=_card_body(allowed_login="warehouse"), headers=h("admin")).json()
    r = send("im-qc", kind="card_action", action={"card_id": only["id"], "action": "confirm"})
    assert r["result"]["outcome"] == "rejected" and "指定的人" in r["replies"][0]["text"]

    # 卡片上没有的按钮
    r = send("im-wh", kind="card_action", action={"card_id": only["id"], "action": "approve"})
    assert r["result"]["outcome"] == "rejected"

    # 不是本租户的卡片短码：当普通文字处理
    r = client.put(
        "/v1/channels/identities",
        json={"channel": "mock", "external_id": "im-o", "login": "other_admin"},
        headers={"Authorization": f"Bearer {keys['other_admin']}"},
    )
    assert r.status_code == 200
    r = client.post(
        "/v1/channels/mock/messages",
        json={"external_msg_id": "o1", "sender_id": "im-o", "text": f"确认 {only['short_code']}"},
        headers={"Authorization": f"Bearer {keys['other_admin']}"},
    )
    assert r.json()["route"] == "router"
    assert (
        client.get(
            f"/v1/cards/{only['id']}", headers={"Authorization": f"Bearer {keys['other_admin']}"}
        ).status_code
        == 404
    )


def test_card_expiry(client, send, bind, h, settings):
    import asyncio

    from sqlalchemy import update

    from fos.db.models import CardRecord, utcnow
    from fos.db.session import make_engine, make_sessionmaker

    bind("im-wh", "warehouse")
    card = client.post("/v1/cards", json=_card_body(), headers=h("admin")).json()

    async def expire():
        engine = make_engine(settings.database_url)
        async with make_sessionmaker(engine)() as s, s.begin():
            await s.execute(
                update(CardRecord)
                .where(CardRecord.id == card["id"])
                .values(expires_at=utcnow() - timedelta(hours=1))
            )
        await engine.dispose()

    asyncio.run(expire())
    r = send("im-wh", kind="card_action", action={"card_id": card["id"], "action": "confirm"})
    assert r["result"]["outcome"] == "expired"


def test_card_api_validation_and_permissions(client, h):
    assert client.post("/v1/cards", json=_card_body(), headers=h("warehouse")).status_code == 403
    no_confirm = _card_body()
    no_confirm["card"]["buttons"] = [{"id": "ok", "label": "好"}]
    assert client.post("/v1/cards", json=no_confirm, headers=h("admin")).status_code == 422
    mock_mismatch = _card_body()
    mock_mismatch["card"]["data_mode"] = "mock"
    assert client.post("/v1/cards", json=mock_mismatch, headers=h("admin")).status_code == 422
    unknown = _card_body()
    unknown["action"]["request"]["type"] = "nope.event"
    assert client.post("/v1/cards", json=unknown, headers=h("admin")).status_code == 422
    ding = _card_body(to={**GROUP, "channel": "dingtalk"})
    assert client.post("/v1/cards", json=ding, headers=h("admin")).status_code == 503  # 凭据未配置
    feishu = _card_body(to={**GROUP, "channel": "feishu"})
    assert client.post("/v1/cards", json=feishu, headers=h("admin")).status_code == 501
