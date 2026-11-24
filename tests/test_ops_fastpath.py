"""运维/网关零模型成本 IM Fast Path 卡片（系统状态 / 网关状态）回归测试。"""

from __future__ import annotations


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_ops_im_fast_path_cards_and_permissions(client, keys) -> None:
    """验证管理员发“系统状态”/“网关状态”零成本直出卡片，非管理员被权限拦截。"""
    for ext_id, login in [("im_ops_admin", "admin"), ("im_ops_sales", "sales")]:
        rb = client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": ext_id, "login": login},
            headers=_h(keys, "admin"),
        )
        assert rb.status_code == 200

    # 非管理员被拒绝
    r_deny = client.post(
        "/v1/channels/mock/messages",
        json={"external_msg_id": "ops_fp_deny", "sender_id": "im_ops_sales", "text": "系统状态"},
        headers=_h(keys, "admin"),
    )
    assert r_deny.status_code == 200
    assert r_deny.json()["route"] == "ops_status"
    assert "platform.ops:admin" in r_deny.json()["replies"][0]["text"]

    # 管理员查询“系统状态”
    r_st = client.post(
        "/v1/channels/mock/messages",
        json={"external_msg_id": "ops_fp_st", "sender_id": "im_ops_admin", "text": "系统状态"},
        headers=_h(keys, "admin"),
    )
    assert r_st.status_code == 200
    b_st = r_st.json()
    assert b_st["route"] == "ops_status" and b_st["status"] == "done"
    card_st = b_st["replies"][0]["card"]
    assert "系统与运维状态" in card_st["title"]
    labels = {f["label"] for f in card_st["fields"]}
    assert {"Token 预算", "厂端网关", "窑炉告警", "IM 覆盖"}.issubset(labels)

    # 管理员查询“网关状态”
    r_gw = client.post(
        "/v1/channels/mock/messages",
        json={"external_msg_id": "ops_fp_gw", "sender_id": "im_ops_admin", "text": "网关状态"},
        headers=_h(keys, "admin"),
    )
    assert r_gw.status_code == 200
    b_gw = r_gw.json()
    assert b_gw["route"] == "ops_fleet" and b_gw["status"] == "done"
    assert "厂端网关车队状态" in b_gw["replies"][0]["card"]["title"]
