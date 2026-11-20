"""W22b 飞书 + 企业微信 IM 适配器测试：加解密、验签、解析、发卡片、附件与 Webhook。"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import httpx
import pytest

from fos.channels import feishu, wecom
from fos.channels.base import ChannelError
from fos.channels.cards import Card, CardButton, CardField
from fos.channels.types import Attachment, ConversationRef

WECOM_AES_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii").rstrip("=")


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def _wecom_sig(token: str, ts: str, nonce: str, enc: str) -> str:
    return hashlib.sha1("".join(sorted([token, ts, nonce, enc])).encode("utf-8")).hexdigest()


def _feishu_sig(key: str, ts: str, nonce: str, raw: bytes) -> str:
    return hashlib.sha256((ts + nonce + key).encode("utf-8") + raw).hexdigest()


# ---------------- 飞书单元测试 ----------------


def test_feishu_crypto_signature_and_parsing():
    key = "feishu-encrypt-key-demo"
    payload = {"type": "url_verification", "challenge": "chal-123", "token": "vt-1"}
    enc = feishu.encrypt_event(key, payload)
    assert feishu.decrypt_event(key, enc) == payload

    with pytest.raises(ChannelError):
        feishu.decrypt_event("wrong-key", enc)

    raw = json.dumps({"encrypt": enc}).encode("utf-8")
    sig = _feishu_sig(key, "1700000000", "nonce-1", raw)
    assert feishu.verify_signature(key, "1700000000", "nonce-1", raw, sig) is True
    assert feishu.verify_signature(key, "1700000000", "nonce-1", raw, "bad") is False

    # 群文本消息去 @_user_1 占位符
    ev = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "e1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_qc1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_g1",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "@_user_1 待检"}),
                "mentions": [{"key": "@_user_1"}],
                "create_time": "1700000000000",
            },
        },
    }
    msg = feishu.parse_event("luneng", ev)
    assert msg is not None and msg.channel == "feishu" and msg.text == "待检"
    assert msg.conversation_type == "group" and msg.mentioned is True

    # 图片消息
    img_ev = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_qc1"}},
            "message": {
                "message_id": "om_2",
                "chat_id": "oc_p2p",
                "chat_type": "p2p",
                "message_type": "image",
                "content": json.dumps({"image_key": "img_abc"}),
            },
        },
    }
    img_msg = feishu.parse_event("luneng", img_ev)
    assert img_msg is not None and img_msg.kind == "image"
    assert img_msg.attachments[0].ref == "om_2:image:img_abc"

    # 富文本消息（post）
    post_ev = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_qc1"}},
            "message": {
                "message_id": "om_3",
                "chat_id": "oc_p2p",
                "chat_type": "p2p",
                "message_type": "post",
                "content": json.dumps(
                    {
                        "zh_cn": {
                            "title": "报检单",
                            "content": [
                                [{"tag": "text", "text": "批号 B-1"}, {"tag": "img", "image_key": "k1"}]
                            ],
                        }
                    }
                ),
            },
        },
    }
    post_msg = feishu.parse_event("luneng", post_ev)
    assert post_msg is not None and "批号 B-1" in post_msg.text
    assert post_msg.attachments[0].ref == "om_3:image:k1"


@pytest.mark.asyncio
async def test_feishu_adapter_send_and_attachment():
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    token_hits = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal token_hits
        if req.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            token_hits += 1
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-fs", "expire": 7200})
        if req.url.path == "/open-apis/im/v1/messages":
            calls.append((req.method, str(req.url), json.loads(req.content)))
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_out"}})
        if req.url.path == "/open-apis/im/v1/messages/om_2/resources/img_abc":
            return httpx.Response(200, content=b"PNGDATA")
        return httpx.Response(404)

    app = feishu.FeishuApp(tenant_id="luneng", app_id="cli_1", app_secret="sec_1")
    adapter = feishu.FeishuAdapter(app, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        to_dm = ConversationRef(
            channel="feishu", conversation_id="oc_1", conversation_type="direct", user_id="ou_1"
        )
        await adapter.send_text(to_dm, "你好")
        card = Card(
            title="确认入库",
            text="请核对",
            fields=[CardField(label="物料", value="莫来石粉")],
            buttons=[CardButton(id="confirm", label="确认入账", style="primary")],
        )
        await adapter.send_card(to_dm, card, card_id="card-1", short_code="K7Q2M")
        att = Attachment(kind="image", ref="om_2:image:img_abc")
        assert await adapter.fetch_attachment(att, max_bytes=1024) == b"PNGDATA"
        with pytest.raises(ChannelError, match="过大"):
            await adapter.fetch_attachment(att, max_bytes=3)
    finally:
        await adapter.aclose()

    assert token_hits == 1  # token 命中缓存
    assert len(calls) == 2
    assert "receive_id_type=open_id" in calls[0][1]
    assert calls[1][2] is not None and calls[1][2]["msg_type"] == "interactive"
    card_body = json.loads(calls[1][2]["content"])
    assert card_body["elements"][1]["actions"][0]["value"] == {"card_id": "card-1", "action": "confirm"}


# ---------------- 企业微信单元测试 ----------------


def test_wecom_crypto_xml_and_xxe_rejection():
    corp_id = "ww_luneng_01"
    token = "wc_tok_1"
    xml_in = (
        "<xml><ToUserName><![CDATA[ww_luneng_01]]></ToUserName>"
        "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[待检]]></Content>"
        "<MsgId>99001</MsgId></xml>"
    )
    enc = wecom.encrypt_message(WECOM_AES_KEY, corp_id, xml_in)
    sig = _wecom_sig(token, "1700000000", "n1", enc)
    assert wecom.verify_signature(token, "1700000000", "n1", enc, sig) is True
    assert wecom.verify_signature(token, "1700000000", "n1", enc, "tampered") is False
    assert wecom.decrypt_message(WECOM_AES_KEY, corp_id, enc) == xml_in

    with pytest.raises(ChannelError, match="CorpID"):
        wecom.decrypt_message(WECOM_AES_KEY, "other_corp", enc)

    with pytest.raises(ChannelError, match="DTD/ENTITY"):
        wecom.parse_xml_message("luneng", "<!DOCTYPE foo [<!ENTITY xxe 'bad'>]><xml/>")

    msg = wecom.parse_xml_message("luneng", xml_in)
    assert msg is not None and msg.channel == "wecom" and msg.sender_id == "zhangsan" and msg.text == "待检"

    card_xml = (
        "<xml><FromUserName><![CDATA[zhangsan]]></FromUserName>"
        "<CreateTime>1700000001</CreateTime><MsgType><![CDATA[event]]></MsgType>"
        "<Event><![CDATA[template_card_event]]></Event>"
        "<TaskId><![CDATA[card-99]]></TaskId><EventKey><![CDATA[confirm]]></EventKey></xml>"
    )
    cmsg = wecom.parse_xml_message("luneng", card_xml)
    assert cmsg is not None and cmsg.kind == "card_action" and cmsg.action is not None
    assert cmsg.action.card_id == "card-99" and cmsg.action.action == "confirm"


@pytest.mark.asyncio
async def test_wecom_adapter_send_and_attachment():
    sent: list[tuple[str, dict[str, Any]]] = []
    token_hits = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal token_hits
        if req.url.path == "/cgi-bin/gettoken":
            token_hits += 1
            return httpx.Response(200, json={"errcode": 0, "access_token": "t-wc", "expires_in": 7200})
        if req.url.path in ("/cgi-bin/message/send", "/cgi-bin/appchat/send"):
            sent.append((req.url.path, json.loads(req.content)))
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        if req.url.path == "/cgi-bin/media/get":
            if req.url.params.get("media_id") == "m_err":
                return httpx.Response(200, json={"errcode": 40007, "errmsg": "invalid media_id"})
            return httpx.Response(200, content=b"JPEGDATA", headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    app = wecom.WeComApp(tenant_id="luneng", corp_id="ww1", agent_id=1000002, secret="sec")
    adapter = wecom.WeComAdapter(app, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        dm = ConversationRef(channel="wecom", conversation_id="u1", conversation_type="direct", user_id="u1")
        grp = ConversationRef(channel="wecom", conversation_id="chat_1", conversation_type="group")
        card = Card(
            title="入库确认",
            text="原料莫来石粉 5 吨",
            fields=[CardField(label="数量", value="5 吨")],
            buttons=[CardButton(id="confirm", label="确认", style="primary")],
        )
        await adapter.send_text(dm, "收到")
        await adapter.send_card(dm, card, card_id="c1", short_code="A2B3C")
        await adapter.send_card(grp, card, card_id="c1", short_code="A2B3C")
        att_ok = Attachment(kind="image", ref="m1")
        assert await adapter.fetch_attachment(att_ok, max_bytes=1024) == b"JPEGDATA"
        with pytest.raises(ChannelError, match="invalid media_id"):
            await adapter.fetch_attachment(Attachment(kind="image", ref="m_err"), max_bytes=1024)
    finally:
        await adapter.aclose()

    assert token_hits == 1
    assert sent[0][0] == "/cgi-bin/message/send" and sent[0][1]["msgtype"] == "text"
    assert sent[1][0] == "/cgi-bin/message/send" and sent[1][1]["msgtype"] == "template_card"
    assert sent[1][1]["template_card"]["task_id"] == "c1"
    assert sent[2][0] == "/cgi-bin/appchat/send" and sent[2][1]["msgtype"] == "markdown"


# ---------------- 端到端 Webhook + L2 卡片确认 ----------------


def test_feishu_and_wecom_webhooks_end_to_end(client, keys):
    fs_sent: list[dict[str, Any]] = []
    wc_sent: list[dict[str, Any]] = []

    def fs_transport(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-fs", "expire": 7200})
        fs_sent.append(json.loads(req.content))
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_r"}})

    def wc_transport(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "t-wc", "expires_in": 7200})
        wc_sent.append(json.loads(req.content))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    fs_app = feishu.FeishuApp(
        tenant_id="luneng",
        app_id="cli_fs",
        app_secret="sec_fs",
        verification_token="vt_fs",
        encrypt_key="ek_fs",
    )
    wc_app = wecom.WeComApp(
        tenant_id="luneng",
        corp_id="ww_luneng",
        agent_id=1000001,
        secret="sec_wc",
        token="tok_wc",
        encoding_aes_key=WECOM_AES_KEY,
    )
    # 同时挂载飞书与企业微信适配器，验证 (tenant_id, channel) 缓存互不覆盖
    client.app.state.adapters[("luneng", "feishu")] = feishu.FeishuAdapter(
        fs_app, http=httpx.AsyncClient(transport=httpx.MockTransport(fs_transport))
    )
    client.app.state.adapters[("luneng", "wecom")] = wecom.WeComAdapter(
        wc_app, http=httpx.AsyncClient(transport=httpx.MockTransport(wc_transport))
    )

    # 绑定账号
    for ch, ext, login in (("feishu", "ou_qc", "qc"), ("wecom", "wc_wh", "warehouse")):
        r = client.put(
            "/v1/channels/identities",
            json={"channel": ch, "external_id": ext, "login": login},
            headers=_h(keys, "admin"),
        )
        assert r.status_code == 200

    # 1. 飞书 URL 校验（加密 + 验签）
    chal_body = {"type": "url_verification", "challenge": "ok-chal", "token": "vt_fs"}
    enc_chal = feishu.encrypt_event("ek_fs", chal_body)
    raw_chal = json.dumps({"encrypt": enc_chal}).encode("utf-8")
    sig = _feishu_sig("ek_fs", "1700000010", "n10", raw_chal)
    r = client.post(
        "/v1/channels/feishu/luneng/webhook",
        content=raw_chal,
        headers={
            "content-type": "application/json",
            "x-lark-request-timestamp": "1700000010",
            "x-lark-request-nonce": "n10",
            "x-lark-signature": sig,
        },
    )
    assert r.status_code == 200 and r.json() == {"challenge": "ok-chal"}

    # 篡改签名被拒 403
    bad_r = client.post(
        "/v1/channels/feishu/luneng/webhook",
        content=raw_chal,
        headers={
            "content-type": "application/json",
            "x-lark-request-timestamp": "1700000010",
            "x-lark-request-nonce": "n10",
            "x-lark-signature": "bad",
        },
    )
    assert bad_r.status_code == 403

    # 2. 飞书文字消息 → 质量待检 Fast Path → 回发互动卡片
    ev = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "fev1", "token": "vt_fs"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_qc"}},
            "message": {
                "message_id": "om_q1",
                "chat_id": "oc_dm1",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "待检"}),
            },
        },
    }
    raw_ev = json.dumps({"encrypt": feishu.encrypt_event("ek_fs", ev)}).encode("utf-8")
    sig_ev = _feishu_sig("ek_fs", "1700000011", "n11", raw_ev)
    r2 = client.post(
        "/v1/channels/feishu/luneng/webhook",
        content=raw_ev,
        headers={
            "content-type": "application/json",
            "x-lark-request-timestamp": "1700000011",
            "x-lark-request-nonce": "n11",
            "x-lark-signature": sig_ev,
        },
    )
    assert r2.status_code == 200 and len(fs_sent) == 1 and fs_sent[0]["msg_type"] == "interactive"

    # 3. 企业微信 GET 回调 URL 校验
    echo_enc = wecom.encrypt_message(WECOM_AES_KEY, "ww_luneng", "hello-wecom")
    echo_sig = _wecom_sig("tok_wc", "1700000020", "n20", echo_enc)
    vr = client.get(
        "/v1/channels/wecom/luneng/webhook",
        params={"msg_signature": echo_sig, "timestamp": "1700000020", "nonce": "n20", "echostr": echo_enc},
    )
    assert vr.status_code == 200 and vr.text == "hello-wecom"

    # 4. 发一张企业微信确认卡片 → 仓管点击模板卡片确认按钮 → 账本真正入账
    card_r = client.post(
        "/v1/cards",
        json={
            "to": {
                "channel": "wecom",
                "conversation_id": "wc_wh",
                "conversation_type": "direct",
                "user_id": "wc_wh",
            },
            "card": {
                "title": "确认新增物料",
                "text": "高铝矾土",
                "buttons": [{"id": "confirm", "label": "确认入账", "style": "primary"}],
            },
            "action": {
                "kind": "ledger.append",
                "request": {
                    "type": "warehouse.material_defined",
                    "object_id": "M-WC-1",
                    "payload": {"name": "高铝矾土", "category": "raw", "unit": "吨"},
                },
            },
        },
        headers=_h(keys, "admin"),
    )
    assert card_r.status_code == 201, card_r.text
    card_id = card_r.json()["id"]
    assert len(wc_sent) == 1 and wc_sent[0]["msgtype"] == "template_card"

    click_xml = (
        "<xml><ToUserName><![CDATA[ww_luneng]]></ToUserName>"
        "<FromUserName><![CDATA[wc_wh]]></FromUserName>"
        "<CreateTime>1700000030</CreateTime><MsgType><![CDATA[event]]></MsgType>"
        "<Event><![CDATA[template_card_event]]></Event>"
        f"<TaskId><![CDATA[{card_id}]]></TaskId><EventKey><![CDATA[confirm]]></EventKey></xml>"
    )
    click_enc = wecom.encrypt_message(WECOM_AES_KEY, "ww_luneng", click_xml)
    click_sig = _wecom_sig("tok_wc", "1700000030", "n30", click_enc)
    wr = client.post(
        "/v1/channels/wecom/luneng/webhook",
        params={"msg_signature": click_sig, "timestamp": "1700000030", "nonce": "n30"},
        content=f"<xml><Encrypt><![CDATA[{click_enc}]]></Encrypt></xml>".encode(),
        headers={"content-type": "application/xml"},
    )
    assert wr.status_code == 200 and wr.text == "success"

    # 验证事件已写入账本，卡片状态变为 confirmed
    obj = client.get("/v1/objects/material/M-WC-1", headers=_h(keys, "warehouse"))
    assert obj.status_code == 200 and obj.json()["state"]["name"] == "高铝矾土"
    c_after = client.get(f"/v1/cards/{card_id}", headers=_h(keys, "admin")).json()
    assert c_after["status"] == "confirmed"
