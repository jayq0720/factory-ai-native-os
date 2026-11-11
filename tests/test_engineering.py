"""工程：技术文件 → 工程项目 → 选材匹配 → 方案 / BOM / 报价 → 审核 → 发布（W10–W12）。"""

from __future__ import annotations

import io
import math

import pytest
import yaml

from fos.engineering.config import EngineeringConfig
from fos.engineering.design import Conditions, QuoteOptions, evaluate
from fos.engineering.heat import HeatError, Layer, solve
from fos.engineering.kb import Price, Product
from fos.engineering.match import customer_spec, search
from fos.engineering.samples import kb_requests
from fos.quickstart import sample_tech_docx
from tests.conftest import REPO

KB_FILE = REPO / "packs/refractory/engineering/sample_kb.yaml"
CFG = EngineeringConfig.model_validate(
    yaml.safe_load((REPO / "packs/refractory/engineering/linings.yaml").read_text(encoding="utf-8"))
)


def _h(k):
    return {"Authorization": f"Bearer {k}"}


# ---------------- 确定性计算 ----------------


def test_heat_constant_k_matches_resistance_network():
    # k 为常数时：q = ΔT / (Σδ/k + 1/h)，逐层温降 = q·δ/k
    r = solve([Layer("a", 230, 0.4), Layer("b", 100, 0.1)], 1200, 25)
    q = r.q
    assert math.isclose(r.temps[1], 1200 - q * 0.23 / 0.4, rel_tol=1e-6)
    assert math.isclose(r.temps[2], r.temps[1] - q * 0.1 / 0.1, rel_tol=1e-6)
    from fos.engineering.heat import surface_h

    ts = r.shell_temp
    assert math.isclose(surface_h(ts, 25) * (ts - 25), q, rel_tol=1e-3)


def test_heat_temperature_dependent_k_and_cylinder():
    flat = solve([Layer("砖", 230, 0.18, 0.0002), Layer("板", 75, 0.035, 0.00015)], 1250, 25)
    # 平均温度下的 k 积分与 q·δ 一致
    t0, t1 = flat.temps[0], flat.temps[1]
    assert math.isclose(0.18 * (t0 - t1) + 0.0001 * (t0**2 - t1**2), flat.q * 0.23, rel_tol=1e-6)
    cyl = solve(
        [Layer("砖", 230, 0.18, 0.0002), Layer("板", 75, 0.035, 0.00015)],
        1250,
        25,
        geometry="cylinder",
        inner_radius_m=1.0,
    )
    assert cyl.shell_temp != flat.shell_temp and cyl.geometry == "cylinder"
    with pytest.raises(HeatError):
        solve([Layer("x", 100, 0.2)], 20, 25)
    with pytest.raises(HeatError):
        solve([Layer("x", 100, 0.2)], 900, 25, geometry="cylinder")


def _kb(mode="real") -> dict[str, Product]:
    data = yaml.safe_load(KB_FILE.read_text(encoding="utf-8"))
    kb = {}
    for p in data["products"]:
        kb[p["code"]] = Product(
            id=p["code"],
            version=1,
            code=p["code"],
            name=p["name"],
            spec=p.get("spec"),
            unit=p["unit"],
            category=p["category"],
            classification_temp=p.get("classification_temp"),
            density=p.get("bulk_density"),
            tc_a=p.get("tc_a"),
            tc_b=p.get("tc_b") or 0.0,
            piece_volume=p.get("piece_volume"),
            loss_rate=p.get("loss_rate", 0),
            unsuitable=[x for x in (p.get("unsuitable_atmospheres") or "").split(",") if x],
            aliases=[x for x in (p.get("aliases") or "").split(",") if x],
            data_mode=mode,
        )
    for pr in data["prices"]:
        kb[pr["code"]].price = Price(
            id="PR-" + pr["code"],
            version=1,
            price=pr["price"],
            unit=pr["unit"],
            cost=pr.get("cost"),
            effective_from=pr["effective_from"],
            valid_until=None,
            data_mode=mode,
        )
    return kb


STATE = {
    "normal_temp": 1250.0,
    "max_temp": 1300.0,
    "atmosphere": "氧化、弱还原",
    "shell_temp": 60.0,
    "ambient_temp": 25.0,
    "area": 277.9,
    "furnace_type": "辊道窑",
    "positions": "烧成带、炉墙、炉顶",
    "thickness_limit": 350.0,
    "thickness_rule": "min",
}


def test_match_respects_hard_constraints():
    kb = _kb()
    cond = Conditions.from_project(STATE, CFG)
    r = search(cond, CFG, kb)
    assert r["alternatives"] and r["alternatives"][0]["label"] == "方案 A"
    for alt in r["alternatives"]:
        d = evaluate(cond, CFG, kb, alt["layers"], QuoteOptions())
        assert d["feasible"], d["checks"]
        hot = kb[alt["layers"][0]["product_id"]]
        assert hot.classification_temp >= 1300 + CFG.safety_margin
        assert d["heat_normal"]["shell_temp"] <= 60
        assert d["total_thickness_mm"] >= 350
    # 分类温度不够的砖被排除并给出原因
    assert any("LN-GL13" in x["reason"] for x in r["rejected"])
    # 含氢气氛：LN-FB1260 不适用
    r2 = search(Conditions.from_project({**STATE, "atmosphere": "含氢"}, CFG), CFG, kb)
    assert all(ly["product_id"] != "LN-FB1260" for a in r2["alternatives"] for ly in a["layers"])


def test_bom_quote_numbers():
    kb = _kb()
    cond = Conditions.from_project(STATE, CFG)
    layers = [{"role": "工作层", "product_id": "LN-ML26", "thickness_mm": 230}]
    d = evaluate(cond, CFG, kb, layers, QuoteOptions(discount_pct=10, freight_per_t=100, tax_rate=13))
    brick, mortar = d["bom"]
    vol = 277.9 * 0.23 * 1.05
    assert brick["qty"] == round(vol * 0.8, 3) and brick["unit"] == "t"
    assert mortar["code"] == "LN-NJ14" and mortar["qty"] == round(vol * 0.12, 3)
    q = d["quote"]
    mat = round(brick["qty"] * 4200, 2) + round(mortar["qty"] * 2200, 2)
    assert math.isclose(q["material_list"], mat, abs_tol=0.01)
    assert math.isclose(q["material_net"], mat - round(mat * 0.1, 2), abs_tol=0.01)
    assert math.isclose(q["total"], round(q["subtotal"] * 1.13, 2), abs_tol=0.02)
    cost = round(brick["qty"] * 3300, 2) + round(mortar["qty"] * 1600, 2)
    assert math.isclose(
        q["gross_margin_pct"], round((q["material_net"] - cost) / q["material_net"] * 100, 2), abs_tol=0.01
    )
    assert q["prices_used"] == ["price_item/PR-LN-ML26@v1", "price_item/PR-LN-NJ14@v1"]
    assert not d["feasible"]  # 单层砖外壁超温、厚度不足


def test_customer_spec_parses_aliases():
    kb = _kb()
    spec = customer_spec("工作层：JM-26 莫来石轻质砖 230 mm；隔热层：LN-1260 硅酸铝纤维板 100 mm", kb)
    assert spec == [
        {"role": "工作层", "product_id": "LN-ML26", "thickness_mm": 230.0},
        {"role": "隔热层", "product_id": "LN-FB1260", "thickness_mm": 100.0},
    ]
    assert customer_spec("工作层：某不存在砖 230 mm", kb) is None


# ---------------- API 全流程 ----------------


def _seed_kb(client, keys, mode="real"):
    for req in kb_requests(KB_FILE, mode):
        r = client.post("/v1/events", json=req.model_dump(mode="json"), headers=_h(keys["kb_admin"]))
        assert r.status_code in (200, 201), r.text


def _tech_doc(client, key, confirm=True, data=None):
    r = client.post(
        "/v1/documents",
        files={"file": ("技术要求.docx", data or sample_tech_docx())},
        data={"kind": "tech"},
        headers=_h(key),
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    if confirm:
        for k in ("normal_temp", "max_temp", "atmosphere", "shell_temp", "area", "total_thickness"):
            r = client.patch(
                f"/v1/documents/{doc_id}/fields/{k}", json={"action": "confirm"}, headers=_h(key)
            )
            assert r.status_code == 200, r.text
    return doc_id


def test_full_flow_to_published_quote(client, keys):
    eng = _h(keys["engineer"])
    _seed_kb(client, keys)
    # 同一份字节上传两次（docx 里带生成时间，各自生成可能跨秒、哈希不同）
    data = sample_tech_docx()
    doc_id = _tech_doc(client, keys["engineer"], confirm=False, data=data)

    # 关键字段没核对不能建项目
    r = client.post("/v1/eng/projects", json={"document_id": doc_id}, headers=eng)
    assert r.status_code == 409 and "最高温度" in r.json()["detail"]
    assert _tech_doc(client, keys["engineer"], data=data) == doc_id  # 同一文件（哈希相同），确认关键字段
    r = client.post("/v1/eng/projects", json={"document_id": doc_id}, headers=eng)
    assert r.status_code == 201, r.text
    p = r.json()["project"]
    assert (p["max_temp"], p["shell_temp"], p["area"], p["ambient_temp"]) == (1300, 60, 277.9, 25)
    assert p["thickness_rule"] == "min" and p["data_mode"] == "real" and p["status"] == "open"
    assert "JM-26" in p["lining_spec"]
    pid = p["id"]
    # 幂等：同一文件再建返回原项目
    r2 = client.post("/v1/eng/projects", json={"document_id": doc_id}, headers=eng)
    assert r2.status_code == 200 and r2.json()["project"]["id"] == pid

    # 证据：字段原文页码
    detail = client.get(f"/v1/eng/projects/{pid}", headers=eng).json()
    ev = detail["history"][0]["evidence"]
    assert any(e["kind"] == "document" and e.get("page") == 1 and "1300℃" in e.get("quote", "") for e in ev)
    assert detail["stage"] == "matching"

    # 匹配
    m = client.post(f"/v1/eng/projects/{pid}/match", headers=eng).json()
    labels = [d["label"] for d in m["designs"]]
    assert labels[0] == "方案 A" and "客户方案" in labels
    cust = next(d for d in m["designs"] if d["label"] == "客户方案")
    assert cust["feasible"] is False  # 客户写的 330 mm 结构外壁超温、厚度不足
    a = next(d for d in m["designs"] if d["label"] == "方案 A")
    assert a["feasible"] and a["shell_temp"] <= 60 and a["data_mode"] == "real"

    full = client.get(f"/v1/eng/designs/{a['id']}", headers=eng).json()
    assert full["proposal"][0]["title"].startswith("一、") and full["data"]["bom"]
    # 改工作层厚度：生成第 2 版，用量与报价重算
    layers = [dict(x) for x in full["inputs"]["layers"]]
    layers[1]["thickness_mm"] = layers[1]["thickness_mm"] + 25
    r = client.patch(f"/v1/eng/designs/{a['id']}", json={"layers": layers, "note": "隔热层加厚"}, headers=eng)
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["version"] == 2 and v2["parent_id"] == a["id"] and v2["total"] > a["total"]
    assert client.patch(f"/v1/eng/designs/{a['id']}", json={}, headers=eng).status_code == 409  # 旧版本不能改

    # 未处理完审核清单不能发布
    r = client.post(f"/v1/eng/designs/{v2['id']}/publish", headers=eng)
    assert r.status_code == 409 and "没有处理" in r.json()["detail"]
    for it in v2["review"]:
        assert not it["blocking"], it
        r = client.patch(
            f"/v1/eng/designs/{v2['id']}/review/{it['id']}",
            json={"disposition": "accepted", "note": "已知"},
            headers=eng,
        )
        assert r.status_code == 200
    assert r.json()["review_summary"]["ready"]

    # 发布 → 命中“销售负责人确认”审批
    r = client.post(f"/v1/eng/designs/{v2['id']}/publish", headers=eng)
    assert r.status_code == 202, r.text
    ap = r.json()["approval"]
    assert [s["name"] for s in ap["steps"]] == ["销售负责人确认"]
    assert client.get(f"/v1/eng/projects/{pid}", headers=eng).json()["stage"] == "reviewing"
    assert client.post(f"/v1/eng/designs/{v2['id']}/publish", headers=eng).status_code == 409
    r = client.post(
        f"/v1/approvals/{ap['id']}/decide", json={"decision": "approve"}, headers=_h(keys["sales_lead"])
    )
    assert r.status_code == 200, r.text

    detail = client.get(f"/v1/eng/projects/{pid}", headers=eng).json()
    assert detail["stage"] == "quoted" and detail["project"]["quote_design"] == v2["id"]
    assert detail["project"]["quote_amount"] == v2["total"]
    assert "price_item/PR-LN-ML26@v1" in detail["project"]["quote_prices"]
    issued = detail["history"][-1]
    assert issued["type"] == "eng.quote_issued" and issued["actor"] == "engineer"  # 以申请人身份入账
    assert any(e["kind"] == "approval" for e in issued["evidence"])
    assert client.get(f"/v1/eng/designs/{v2['id']}", headers=eng).json()["status"] == "published"

    # 导出
    from docx import Document
    from openpyxl import load_workbook

    x = client.get(f"/v1/eng/designs/{v2['id']}/export?format=xlsx", headers=eng)
    assert x.status_code == 200
    wb = load_workbook(io.BytesIO(x.content))
    assert wb.sheetnames == ["BOM", "报价", "审核"]
    w = client.get(f"/v1/eng/designs/{v2['id']}/export?format=docx", headers=eng)
    text = "\n".join(p.text for p in Document(io.BytesIO(w.content)).paragraphs)
    assert "传热校核" in text and "工程概况" in text

    # 销售能看不能改；工人看不到
    assert client.get(f"/v1/eng/projects/{pid}", headers=_h(keys["sales"])).status_code == 200
    assert client.post(f"/v1/eng/projects/{pid}/match", headers=_h(keys["sales"])).status_code == 403
    assert client.get("/v1/eng/projects", headers=_h(keys["worker"])).status_code == 403
    # 租户隔离
    assert client.get(f"/v1/eng/projects/{pid}", headers=_h(keys["other_admin"])).status_code == 404
    assert client.get(f"/v1/eng/designs/{v2['id']}", headers=_h(keys["other_admin"])).status_code == 404


def _ready_design(client, keys, label="方案 A"):
    eng = _h(keys["engineer"])
    doc_id = _tech_doc(client, keys["engineer"])
    pid = client.post("/v1/eng/projects", json={"document_id": doc_id}, headers=eng).json()["project"]["id"]
    m = client.post(f"/v1/eng/projects/{pid}/match", headers=eng).json()
    return pid, next(d for d in m["designs"] if d["label"] == label)


def _accept_all(client, eng, design):
    for it in design["review"]:
        if not it["blocking"]:
            client.patch(
                f"/v1/eng/designs/{design['id']}/review/{it['id']}",
                json={"disposition": "accepted"},
                headers=eng,
            )


def test_margin_rules_and_below_cost(client, keys):
    eng = _h(keys["engineer"])
    _seed_kb(client, keys)
    _pid, a = _ready_design(client, keys)
    # 打 15 折扣 → 毛利率 < 15% → 加总经理审批
    low = client.patch(
        f"/v1/eng/designs/{a['id']}", json={"options": {"discount_pct": 15}}, headers=eng
    ).json()
    assert low["gross_margin_pct"] < 15
    assert any(i["key"] == "low_margin" for i in low["review"])
    _accept_all(client, eng, low)
    r = client.post(f"/v1/eng/designs/{low['id']}/publish", headers=eng)
    assert r.status_code == 202
    assert [s["name"] for s in r.json()["approval"]["steps"]] == ["销售负责人确认", "总经理审批"]
    # 驳回后可以改了重新提交
    ap = r.json()["approval"]["id"]
    client.post(f"/v1/approvals/{ap}/decide", json={"decision": "reject"}, headers=_h(keys["sales_lead"]))
    # 低于成本：审核清单里是必须修正的条目，不能发布
    bad = client.patch(
        f"/v1/eng/designs/{low['id']}", json={"options": {"discount_pct": 40}}, headers=eng
    ).json()
    item = next(i for i in bad["review"] if i["key"] == "below_cost")
    assert item["blocking"]
    r = client.patch(
        f"/v1/eng/designs/{bad['id']}/review/{item['id']}", json={"disposition": "accepted"}, headers=eng
    )
    assert r.status_code == 409
    _accept_all(client, eng, bad)
    r = client.post(f"/v1/eng/designs/{bad['id']}/publish", headers=eng)
    assert r.status_code == 409 and "必须修正" in r.json()["detail"]


def test_project_change_makes_designs_stale(client, keys):
    eng = _h(keys["engineer"])
    _seed_kb(client, keys)
    pid, a = _ready_design(client, keys)
    r = client.patch(f"/v1/eng/projects/{pid}", json={"shell_temp": 70, "note": "客户电话确认"}, headers=eng)
    assert r.status_code == 200, r.text
    d = client.get(f"/v1/eng/projects/{pid}", headers=eng).json()["designs"]
    assert all(x["stale"] for x in d)
    _accept_all(client, eng, client.get(f"/v1/eng/designs/{a['id']}", headers=eng).json())
    r = client.post(f"/v1/eng/designs/{a['id']}/publish", headers=eng)
    assert r.status_code == 409 and "重新计算" in r.json()["detail"]
    v2 = client.patch(f"/v1/eng/designs/{a['id']}", json={}, headers=eng).json()  # 重算
    assert v2["stale"] is False and v2["data"]["conditions"]["shell_temp"] == 70


def test_mock_kb_cannot_publish_or_export(client, keys):
    eng = _h(keys["engineer"])
    _seed_kb(client, keys, mode="mock")
    _pid, a = _ready_design(client, keys)
    assert a["data_mode"] == "mock"
    full = client.get(f"/v1/eng/designs/{a['id']}", headers=eng).json()
    mock_item = next(i for i in full["review"] if i["key"] == "mock")
    assert mock_item["blocking"]
    assert client.get(f"/v1/eng/designs/{a['id']}/export?format=xlsx", headers=eng).status_code == 409
    assert client.get(f"/v1/eng/designs/{a['id']}/export?format=json", headers=eng).status_code == 200
    _accept_all(client, eng, full)
    r = client.post(f"/v1/eng/designs/{a['id']}/publish", headers=eng)
    assert r.status_code == 409


def test_calc_and_kb_endpoints(client, keys):
    eng = _h(keys["engineer"])
    _seed_kb(client, keys)
    r = client.post(
        "/v1/eng/calc",
        json={"layers": [{"thickness_mm": 230, "tc_a": 0.18, "tc_b": 0.0002}], "hot_temp": 1250},
        headers=eng,
    )
    assert r.status_code == 200 and r.json()["shell_temp"] > 25
    items = client.get("/v1/kb/products", headers=eng).json()["items"]
    ml26 = next(p for p in items if p["code"] == "LN-ML26")
    assert ml26["thermal_ready"] and ml26["price"]["price"] == 4200 and ml26["aliases"] == ["JM-26", "JM26"]
    assert client.get("/v1/kb/linings", headers=eng).json()["safety_margin"] == 100
    assert client.get("/v1/kb/products", headers=_h(keys["worker"])).status_code == 403
    # 调价是一条事件，版本号加 1
    r = client.post(
        "/v1/events",
        json={"type": "kb.price_changed", "object_id": "PR-LN-ML26", "payload": {"price": 4300}},
        headers=_h(keys["kb_admin"]),
    )
    assert r.status_code == 201 and r.json()["version"] == 2
