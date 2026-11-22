"""W28 统一 Watchdog 巡检、安全基线自检、SaaS 多租户运营与第二家工厂零代码复制验证测试。"""

from __future__ import annotations

import textwrap
from datetime import date, timedelta
from pathlib import Path

from fos.ops.service import verify_tenant_replication

REPO = Path(__file__).resolve().parents[1]


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_unified_watchdog_and_saas_ops(client, keys):
    today = date.today()
    overdue = (today - timedelta(days=2)).isoformat()
    exp_soon = (today + timedelta(days=5)).isoformat()
    issued = (today - timedelta(days=200)).isoformat()

    # 1. 权限校验：普通销售无权访问运营总控（403）
    assert client.get("/v1/ops/health", headers=_h(keys, "sales")).status_code == 403
    assert client.post("/v1/ops/watchdog/run", headers=_h(keys, "sales")).status_code == 403

    # 2. 在 EHS 和 HR 分别制造一条超期隐患与一条临期特种证照
    assert (
        client.post(
            "/v1/ehs/hazards",
            json={
                "location": "2# 窑尾",
                "category": "fire",
                "level": "major",
                "description": "灭火器压力不足",
                "due_date": overdue,
            },
            headers=_h(keys, "engineer"),
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/hr/employees",
            json={
                "emp_no": "EMP-WD1",
                "name": "赵焊工",
                "department": "维修班",
                "position": "焊工",
                "hire_date": issued,
            },
            headers=_h(keys, "shift_lead"),
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/v1/hr/certs",
            json={
                "cert_no": "WELD-WD1",
                "employee": "EMP-WD1",
                "kind": "welder",
                "issued_date": issued,
                "expires_date": exp_soon,
            },
            headers=_h(keys, "shift_lead"),
        ).status_code
        == 201
    )

    # 3. 执行统一 Watchdog 巡检：第一次新建 2 条待办（EHS + HR），第二次幂等为 0
    r1 = client.post("/v1/ops/watchdog/run", headers=_h(keys, "admin"))
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    assert d1["modules"]["ehs_hazards"] == 1 and d1["modules"]["hr_certs"] == 1
    assert d1["total_todos_created"] == 2

    r2 = client.post("/v1/ops/watchdog/run", headers=_h(keys, "admin"))
    assert r2.status_code == 200 and r2.json()["total_todos_created"] == 0

    # 4. 租户健康快照、安全基线与多租户 SaaS 总览
    hl = client.get("/v1/ops/health", headers=_h(keys, "admin")).json()
    assert hl["tenant_id"] == "luneng" and hl["counts"]["open_todos"] >= 2
    assert {"ehs", "hr", "rnd", "construction", "market"}.issubset(set(hl["enabled_modules"]))

    sec = client.get("/v1/ops/security", headers=_h(keys, "admin")).json()
    assert sec["passed"] is True and sec["skills_count"] >= 8

    ov = client.get("/v1/ops/tenants", headers=_h(keys, "admin")).json()
    assert ov["tenant_count"] == 2  # luneng + other
    assert ov["totals"]["open_todos"] >= 2

    rep = client.get("/v1/ops/replication/other", headers=_h(keys, "admin")).json()
    assert rep["ready"] is True and rep["industry_pack"] == "refractory"


def test_second_factory_zero_code_replication(tmp_path: Path):
    """验证第二家同行业工厂仅靠 tenant.yaml 即可零代码复用全套本体、策略与 Skill。"""
    tdir = tmp_path / "tenants"
    (tdir / "huadong-ref").mkdir(parents=True)
    (tdir / "huadong-ref" / "tenant.yaml").write_text(
        textwrap.dedent(
            """
            id: huadong-ref
            name: 华东耐材二厂
            industry_pack: refractory
            timezone: Asia/Shanghai
            im: [dingtalk, feishu]
            roles:
              admin: {name: 管理员, permissions: ["*.*:*"]}
              gm: {name: 总经理, permissions: ["*.*:read", "*.*:approve"]}
              sales_lead: {name: 销售负责人, permissions: ["sales.*:*", "market.*:*", "eng.quote:approve"]}
              engineer: {name: 工程师, permissions: ["eng.*:*", "eam.*:*", "ehs.*:*", "construction.*:*"]}
              shift_lead: {name: 班组长, permissions: ["production.*:*", "eam.*:*", "ehs.*:*", "hr.*:*"]}
              rnd: {name: 研发, permissions: ["rnd.*:*"]}
            """
        ),
        encoding="utf-8",
    )
    res = verify_tenant_replication(packs_dir=REPO / "packs", tenants_dir=tdir, tenant_id="huadong-ref")
    assert res["ready"] is True
    assert res["objects_count"] >= 25 and res["events_count"] >= 45
    assert res["skills_count"] >= 8
    assert "pack:refractory/ehs" in res["ontology_layers"]
    assert "pack:refractory/market" in res["ontology_layers"]
