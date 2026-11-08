"""招标拆解：招标文件 → 招标项目 → 需求矩阵 / 偏离表 / 评分点 / 资料清单 → 转工程项目 → 提交投标（W18）。"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

from fos.perception.parser import _parse_docx
from fos.quickstart import sample_tender_docx
from fos.tender import decompose as dc
from fos.tender.config import TenderConfig
from fos.tender.notify import due_offsets
from fos.tender.samples import credential_requests
from fos.tender.service import parse_money, parse_time
from tests.conftest import REPO
from tests.test_engineering import _seed_kb

TZ = ZoneInfo("Asia/Shanghai")
CFG = TenderConfig.model_validate(
    yaml.safe_load((REPO / "packs/refractory/tender.yaml").read_text(encoding="utf-8"))
)
CRED_FILE = REPO / "packs/refractory/tender/sample_credentials.yaml"


def _h(k):
    return {"Authorization": f"Bearer {k}"}


def _lines():
    return dc.lines_of([(1, _parse_docx(sample_tender_docx()).markdown)], CFG)


# ---------------- 确定性拆解 ----------------


def test_structure_and_clauses():
    lines, sections = _lines()
    assert [s["id"] for s in sections] == [
        "notice",
        "instructions",
        "evaluation",
        "contract",
        "technical",
        "format",
    ]
    items = dc.clauses(lines, CFG)
    by_text = {c["text"]: c for c in items}
    star = by_text["★ 投标产品分类温度不得低于 1400℃。"]
    assert star["level"] == "star" and star["disqualify"] and star["category"] == "技术"
    assert star["section"] == "instructions" and star["owner_role"] == "engineer"
    # ★ 只认原文标记：两条★全部召回
    assert sum(c["level"] == "star" for c in items) == 2
    assert by_text["▲ 质保期不少于 12 个月。"]["level"] == "key"
    assert by_text["投标文件未按要求密封的，按无效投标处理。"]["disqualify"]
    assert by_text["投标文件未按要求密封的，按无效投标处理。"]["category"] == "格式"
    assert by_text["5.7 每批次产品须提供出厂检验报告。"]["no"] == "5.7"
    # 陈述性的关键信息、得分项、工况描述不进需求矩阵
    assert not any("投标截止时间" in t or "得 2 分" in t or "正常工作温度" in t for t in by_text)
    # “供应商”“须知”里的“应”“须”不算要求
    assert not dc.is_requirement("第二章 供应商须知说明", CFG)


def test_scoring_and_documents():
    lines, _ = _lines()
    score = dc.scoring(lines, CFG)
    assert [s["points"] for s in score[:3]] == [50, 20, 30]
    perf = next(s for s in score if "业绩" in s["item"])
    assert perf["points"] == 10 and perf["evidence"] == ["performance"]  # “最多 10 分”
    docs = {d["id"]: d for d in dc.requested_documents(lines, CFG)}
    assert docs["test_report"]["max_age_months"] == 12 and docs["test_report"]["star"]
    assert docs["performance"]["min_count"] == 2
    assert "min_count" not in docs["license"]
    assert {"bid_letter", "authorization", "bid_bond", "certificate"} <= set(docs)


def test_requirement_parsing_and_compare():
    caps = {c.id: c for c in CFG.capabilities}
    r = dc.parse_requirement("★ 投标产品分类温度不得低于 1400℃。", caps["classification_temp"])
    assert (r["rule"], r["value"]) == ("min", 1400)
    assert dc.compare(r, 1430, caps["classification_temp"]) == "正偏离"
    assert dc.compare(r, 1400, caps["classification_temp"]) == "无偏离"
    assert dc.compare(r, 1260, caps["classification_temp"]) == "负偏离"
    assert dc.compare(r, None, caps["classification_temp"]) == "待确认"
    w = dc.parse_requirement("质保期：一年", caps["warranty"])
    assert (w["rule"], w["value"]) == ("min", 12)
    d = dc.parse_requirement("交货期：合同签订后 45 日历天", caps["delivery"])
    assert (d["rule"], d["value"]) == ("max", 45) and dc.compare(d, 30, caps["delivery"]) == "正偏离"
    assert dc.parse_requirement("质保期满无质量问题支付 10%。", caps["warranty"]) is None  # 10% 不是质保期
    assert dc.parse_requirement("近三年业绩合同复印件、第三方检测报告", caps["report_age"]) is None
    assert dc.parse_requirement("须提供近一年内的第三方检测报告", caps["report_age"])["value"] == 12
    assert dc.parse_requirement("1400℃以上的分类温度", caps["classification_temp"]) is None
    assert dc.parse_requirement("分类温度 1400℃以上", caps["classification_temp"])["rule"] == "min"


def test_value_parsing_and_reminder_offsets():
    assert parse_money("186.5", "万元") == 1865000 and parse_money("3万元") == 30000
    t = parse_time("2026年12月18日09:30", TZ)
    assert t == datetime(2026, 12, 18, 9, 30, tzinfo=TZ)
    assert parse_time("2026-12-08", TZ).hour == 0
    dl = datetime(2026, 12, 18, 9, 30, tzinfo=TZ)
    assert due_offsets(dl, dl - timedelta(hours=100), [72, 24, 3]) == []
    assert due_offsets(dl, dl - timedelta(hours=30), [72, 24, 3]) == [72]
    assert due_offsets(dl, dl - timedelta(hours=2), [72, 24, 3]) == [3, 24, 72]
    assert due_offsets(dl, dl + timedelta(minutes=1), [72, 24, 3]) == []


# ---------------- API 全流程 ----------------


def _seed_creds(client, keys, mode="real"):
    for req in credential_requests(CRED_FILE, date.today(), mode):
        r = client.post("/v1/events", json=req.model_dump(mode="json"), headers=_h(keys["kb_admin"]))
        assert r.status_code in (200, 201), r.text


def _tender_doc(client, key, confirm=("bid_deadline",)):
    r = client.post(
        "/v1/documents",
        files={"file": ("招标文件.docx", sample_tender_docx())},
        data={"kind": "tender"},
        headers=_h(key),
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    for k in confirm:
        r = client.patch(f"/v1/documents/{doc_id}/fields/{k}", json={"action": "confirm"}, headers=_h(key))
        assert r.status_code == 200, r.text
    return doc_id


def _deadline(days=20):
    return (datetime.now(TZ) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _register(client, keys, who="engineer", **extra):
    doc_id = _tender_doc(client, keys[who])
    body = {"document_id": doc_id, "overrides": {"bid_deadline": _deadline()}, **extra}
    r = client.post("/v1/tenders", json=body, headers=_h(keys[who]))
    assert r.status_code == 201, r.text
    return doc_id, r.json()


def _handle_all(client, h, analysis):
    for it in analysis["items"]:
        if it["status"] == "open" and (it["level"] != "normal" or it["disqualify"]):
            r = client.patch(
                f"/v1/tender-analyses/{analysis['id']}/items/{it['id']}",
                json={"status": "responded"},
                headers=h,
            )
            assert r.status_code == 200, r.text
    return r.json()["summary"]


def test_full_flow_register_decompose_submit(client, keys):
    eng, lead = _h(keys["engineer"]), _h(keys["sales_lead"])
    _seed_kb(client, keys)
    _seed_creds(client, keys)

    # 截止时间没人工核对不能登记
    doc_id = _tender_doc(client, keys["engineer"], confirm=())
    r = client.post("/v1/tenders", json={"document_id": doc_id}, headers=eng)
    assert r.status_code == 409 and "投标截止时间" in r.json()["detail"]

    doc_id, out = _register(client, keys)
    t, a = out["tender"], out["analysis"]
    assert (t["tender_no"], t["budget"], t["bid_bond"], t["status"]) == (
        "ZB-2026-0458",
        1865000,
        30000,
        "open",
    )
    assert t["owner"] == "engineer" and t["data_mode"] == "real" and a["data_mode"] == "real"
    tid = t["id"]
    # 幂等：同一文件再登记返回原项目
    r = client.post("/v1/tenders", json={"document_id": doc_id}, headers=eng)
    assert r.status_code == 200 and r.json()["tender"]["id"] == tid

    s = a["summary"]
    assert s["star"] == 2 and s["star_open"] == 2 and not s["ready"]
    # 偏离表：分类温度取知识库最好的（尚未选定产品）；交货期、质保期来自关键信息与条款
    devs = {d["capability"]: d for d in a["deviations"]}
    assert (
        devs["classification_temp"]["status"] == "正偏离" and "1550" in devs["classification_temp"]["basis"]
    )
    assert devs["warranty"]["status"] == "无偏离"
    assert devs["delivery"]["status"] == "正偏离" and devs["delivery"]["no"] == "关键信息"
    assert devs["report_age"]["status"] == "正偏离"  # 最新检测报告约 3 个月 ≤ 12 个月
    # 资料清单：资料库对上；纤维板检测报告超过一年时效但有另一份有效的
    docs = {d["id"]: d for d in a["checklist"]}
    assert docs["license"]["status"] == "ok" and docs["performance"]["status"] == "ok"
    assert docs["test_report"]["status"] == "ok"
    assert any(m["problem"] and "时效" in m["problem"] for m in docs["test_report"]["matches"])
    assert docs["authorization"]["status"] == "prepare"
    assert a["scoring"][0]["points"] == 50
    # 截止提醒：登记时生成待办
    detail = client.get(f"/v1/tenders/{tid}", headers=eng).json()
    assert any(x["title"].startswith("投标截止") for x in detail["reminders"])
    ev = detail["history"][0]["evidence"]
    assert any(e["kind"] == "document" and "ZB-2026-0458" in (e.get("quote") or "") for e in ev)
    inbox = client.get("/v1/inbox", headers=eng).json()
    assert any("投标截止" in (i.get("title") or "") for i in inbox["items"])

    # 未处理★条款不能提交
    r = client.post(f"/v1/tender-analyses/{a['id']}/submit", headers=eng)
    assert r.status_code == 409 and "没有处理" in r.json()["detail"]

    # 转工程项目：技术规格的关键工况要先核对
    r = client.post(f"/v1/tenders/{tid}/eng-project", headers=eng)
    assert r.status_code == 409 and "字段核对" in r.json()["detail"]
    # 在登记用的那份文档上确认（重新生成的 docx 带保存时间，哈希可能不同，不能靠重新上传）
    for k in ("normal_temp", "max_temp", "atmosphere", "shell_temp", "area", "total_thickness"):
        r = client.patch(f"/v1/documents/{doc_id}/fields/{k}", json={"action": "confirm"}, headers=eng)
        assert r.status_code == 200, r.text
    r = client.post(f"/v1/tenders/{tid}/eng-project", headers=eng)
    assert r.status_code == 201, r.text
    pid = r.json()["eng_project"]
    p = client.get(f"/v1/eng/projects/{pid}", headers=eng).json()["project"]
    assert (p["max_temp"], p["area"], p["customer_name"], p["thickness_rule"]) == (
        1320,
        180,
        "某钢铁股份有限公司",
        "max",
    )
    assert client.post(f"/v1/tenders/{tid}/eng-project", headers=eng).json()["created"] is False
    # 销售负责人没有工程写权限
    assert client.post(f"/v1/tenders/{tid}/eng-project", headers=lead).status_code == 403

    # 招标项目变了 → 旧拆解过期，重新拆解时人工处理结果保留
    first = a["items"][0]
    client.patch(
        f"/v1/tender-analyses/{a['id']}/items/{first['id']}",
        json={"status": "responded", "response": "我方具备独立法人资格，提供 2 项业绩", "assignee": "sales"},
        headers=eng,
    )
    assert client.get(f"/v1/tender-analyses/{a['id']}", headers=eng).json()["stale"]
    a2 = client.post(f"/v1/tenders/{tid}/analyze", headers=eng).json()
    assert a2["version"] == 2 and not a2["stale"]
    kept = next(i for i in a2["items"] if i["id"] == first["id"])
    assert kept["status"] == "responded" and kept["assignee"] == "sales" and "独立法人" in kept["response"]
    assert (
        client.patch(
            f"/v1/tender-analyses/{a['id']}/items/{first['id']}", json={"status": "na"}, headers=eng
        ).status_code
        == 409
    )

    # 导出 Excel
    from openpyxl import load_workbook

    x = client.get(f"/v1/tender-analyses/{a2['id']}/export", headers=eng)
    assert x.status_code == 200
    assert load_workbook(io.BytesIO(x.content)).sheetnames == ["需求矩阵", "偏离表", "资料清单", "评分点"]

    # 处理完 → 提交投标：命中销售负责人确认 + 50 万以上总经理审批
    s = _handle_all(client, eng, a2)
    assert s["ready"] and s["must_open"] == 0
    r = client.post(f"/v1/tender-analyses/{a2['id']}/submit", json={"bid_amount": 1500000}, headers=eng)
    assert r.status_code == 202, r.text
    ap = r.json()["approval"]
    assert [x["name"] for x in ap["steps"]] == ["销售负责人确认", "总经理审批"]
    assert client.post(f"/v1/tenders/{tid}/analyze", headers=eng).status_code == 409  # 审批中不能重新拆解
    for who in ("sales_lead", "gm"):
        r = client.post(
            f"/v1/approvals/{ap['id']}/decide", json={"decision": "approve"}, headers=_h(keys[who])
        )
        assert r.status_code == 200, r.text
    detail = client.get(f"/v1/tenders/{tid}", headers=eng).json()
    t = detail["tender"]
    assert t["status"] == "submitted" and t["analysis"] == a2["id"] and t["bid_amount"] == 1500000
    assert t["star_total"] == 2 and t["star_negative"] == 0
    sub = detail["history"][-1]
    assert sub["type"] == "sales.tender_submitted" and sub["actor"] == "engineer"
    assert any(e["kind"] == "approval" for e in sub["evidence"])
    assert detail["analysis"]["status"] == "accepted"

    # 结果
    r = client.post(
        f"/v1/tenders/{tid}/close", json={"status": "won", "note": "第一中标候选人"}, headers=lead
    )
    assert r.status_code == 200 and r.json()["tender"]["status"] == "won"

    # 权限与租户隔离
    assert client.get("/v1/tenders", headers=_h(keys["worker"])).status_code == 403
    assert client.get(f"/v1/tenders/{tid}", headers=_h(keys["sales"])).status_code == 200
    assert client.get(f"/v1/tenders/{tid}", headers=_h(keys["other_admin"])).status_code == 404
    assert client.get(f"/v1/tender-analyses/{a2['id']}", headers=_h(keys["other_admin"])).status_code == 404
    assert len(client.get("/v1/credentials", headers=_h(keys["kb_admin"])).json()["items"]) == 6


def test_star_negative_is_red_line_and_missing_docs_block(client, keys):
    eng = _h(keys["engineer"])
    _seed_kb(client, keys)
    _, out = _register(client, keys)  # 资料库是空的
    a = out["analysis"]
    docs = {d["id"]: d for d in a["checklist"]}
    assert docs["license"]["status"] == "missing"
    assert any(r["type"] == "资料缺失" for r in a["risks"])
    _handle_all(client, eng, a)
    r = client.post(f"/v1/tender-analyses/{a['id']}/submit", headers=eng)
    assert r.status_code == 409 and "资料清单" in r.json()["detail"]
    for d in a["checklist"]:
        if d["status"] in ("missing", "expired", "insufficient"):
            r = client.patch(
                f"/v1/tender-analyses/{a['id']}/documents/{d['id']}",
                json={"disposition": "prepared", "note": "线下已办"},
                headers=eng,
            )
            assert r.status_code == 200, r.text
    # 技术人员判定★分类温度条款负偏离（例如所报产品只有 1260℃）→ 红线
    star = next(i for i in a["items"] if "分类温度" in i["text"])
    r = client.patch(
        f"/v1/tender-analyses/{a['id']}/items/{star['id']}",
        json={"deviation": "负偏离", "status": "accepted", "note": "所报纤维板 1260℃"},
        headers=eng,
    )
    assert r.json()["summary"]["star_negative"] == 1
    view = client.get(f"/v1/tender-analyses/{a['id']}", headers=eng).json()
    assert any(x["type"] == "★负偏离" and x["level"] == "block" for x in view["risks"])
    r = client.post(f"/v1/tender-analyses/{a['id']}/submit", headers=eng)
    assert r.status_code == 403 and "红线" in r.json()["detail"]
    # 放弃投标：提醒待办关掉
    r = client.post(f"/v1/tenders/{a['tender_id']}/close", json={"status": "abandoned"}, headers=eng)
    assert r.status_code == 200
    rem = client.get(f"/v1/tenders/{a['tender_id']}", headers=eng).json()["reminders"]
    assert rem and all(x["status"] == "dismissed" for x in rem)


def test_mock_credentials_cannot_submit_or_export(client, keys):
    eng = _h(keys["engineer"])
    _seed_kb(client, keys)
    _seed_creds(client, keys, mode="mock")
    _, out = _register(client, keys)
    a = out["analysis"]
    assert a["data_mode"] == "mock" and a["notice"]
    _handle_all(client, eng, a)
    assert client.post(f"/v1/tender-analyses/{a['id']}/submit", headers=eng).status_code == 409
    assert client.get(f"/v1/tender-analyses/{a['id']}/export", headers=eng).status_code == 409


def test_deadline_change_regenerates_reminders_and_im(client, keys):
    eng = _h(keys["engineer"])
    _seed_kb(client, keys)
    _, out = _register(client, keys)
    tid = out["tender"]["id"]
    new = _deadline(25)
    r = client.patch(
        f"/v1/tenders/{tid}", json={"bid_deadline": new, "note": "补遗：截止时间延后"}, headers=eng
    )
    assert r.status_code == 200, r.text
    rem = client.get(f"/v1/tenders/{tid}", headers=eng).json()["reminders"]
    open_bid = [x for x in rem if x["status"] == "open" and x["title"].startswith("投标截止")]
    assert len(open_bid) == 1 and datetime.fromisoformat(open_bid[0]["due_at"]) == datetime.fromisoformat(new)
    assert any(x["status"] == "dismissed" for x in rem)

    # IM 只读指令
    for who, ext in (("engineer", "im-eng"), ("worker", "im-w")):
        client.put(
            "/v1/channels/identities",
            json={"channel": "mock", "external_id": ext, "login": who},
            headers=_h(keys["admin"]),
        )

    def say(i, text, sender="im-eng"):
        body = {"external_msg_id": f"t{i}", "sender_id": sender, "kind": "text", "text": text}
        return client.post("/v1/channels/mock/messages", json=body, headers=_h(keys["admin"])).json()

    o = say(1, "招标")
    assert o["route"] == "tender" and tid in o["replies"][0]["card"]["fields"][0]["label"]
    card = say(2, "招标 ZB-2026-0458")["replies"][0]["card"]
    assert card["title"].startswith("招标：") and any(f["label"] == "★未响应" for f in card["fields"])
    assert "没有找到" in say(3, "招标 XX-1")["replies"][0]["text"]
    assert "没有查看招标项目的权限" in say(4, "招标", sender="im-w")["replies"][0]["text"]

    # 截止前提醒：到点只推一次
    from fos.tender.notify import remind

    svc = client.app.state.services
    adapter = client.app.state.mock_channel
    client.put(
        "/v1/channels/identities",
        json={"channel": "mock", "external_id": "im-lead", "login": "sales_lead"},
        headers=_h(keys["admin"]),
    )
    at = datetime.fromisoformat(new) - timedelta(hours=20)
    n0 = len(adapter.outbox)
    done = client.portal.call(remind, svc, "luneng", adapter, at)
    assert done == [f"{tid}:bid_deadline:24"]
    sent = adapter.outbox[n0:]
    assert len(sent) == 2 and all("投标截止还有 20 小时" in str(m) for m in sent)  # 负责人 + 销售负责人
    assert client.portal.call(remind, svc, "luneng", adapter, at + timedelta(minutes=30)) == []
