"""全厂月度经营与合规汇总报表（API + Excel + CLI）回归测试。"""

from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path

import pytest
from openpyxl import load_workbook

from fos.cli import main
from fos.config import get_settings


def _h(keys: dict[str, str], who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[who]}"}


def test_monthly_report_api_and_excel_export(client, keys) -> None:
    """验证月度经营报表 JSON/Markdown、三表 Excel 导出及规则 4（过滤模拟数据）。"""
    today = date.today()
    month = today.strftime("%Y-%m")
    close_dt = (today + timedelta(days=20)).isoformat()

    # 写入一条真实商机 + 一条模拟商机，验证正式月报只统计真实数据
    r_real = client.post(
        "/v1/market/leads",
        json={
            "customer_name": "海螺水泥",
            "industry": "cement",
            "product_interest": "镁铝尖晶石砖",
            "est_amount": 400000.0,
            "probability": 50,
            "owner_name": "王销",
            "stage": "quoted",
            "expected_close_date": close_dt,
            "data_mode": "real",
        },
        headers=_h(keys, "sales"),
    )
    assert r_real.status_code == 201
    r_mock = client.post(
        "/v1/market/leads",
        json={
            "customer_name": "模拟钢厂",
            "industry": "steel",
            "product_interest": "高铝砖",
            "est_amount": 900000.0,
            "probability": 100,
            "owner_name": "测试",
            "stage": "quoted",
            "expected_close_date": close_dt,
            "data_mode": "mock",
        },
        headers=_h(keys, "sales"),
    )
    assert r_mock.status_code == 201

    # 非法月份返回 422
    r_bad = client.get("/v1/market/monthly-report?month=2026-13", headers=_h(keys, "gm"))
    assert r_bad.status_code == 422

    # 正常查询 JSON + Markdown
    r = client.get(f"/v1/market/monthly-report?month={month}", headers=_h(keys, "gm"))
    assert r.status_code == 200
    body = r.json()
    assert body["data_mode"] == "real"
    assert body["month"] == month
    assert body["kpis"]["weighted_pipeline_amount"] == 200000.0
    assert body["activity"]["events_total"] >= 1
    assert "market" in body["activity"]["domain_counts"]
    assert f"{month} 经营与合规月报" in body["markdown"]

    # 正式三表 Excel 导出
    rx = client.get(f"/v1/market/monthly-report.xlsx?month={month}", headers=_h(keys, "gm"))
    assert rx.status_code == 200
    wb = load_workbook(io.BytesIO(rx.content))
    assert wb.sheetnames == ["经营总览", "分域事件统计", "风险与整改项"]


def test_cli_monthly_report_with_xlsx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """验证 fos monthly-report CLI 命令输出 Markdown 并落盘合法 Excel 文件。"""
    db_file = tmp_path / "monthly_cli.db"
    out_xlsx = tmp_path / "out" / "report.xlsx"
    monkeypatch.setenv("FOS_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    try:
        assert main(["init-db"]) == 0
        rc = main(["monthly-report", "luneng", "--month", "2026-11", "--xlsx", str(out_xlsx)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "2026-11 经营与合规月报" in out
        assert out_xlsx.exists()
        wb = load_workbook(out_xlsx)
        assert wb.sheetnames == ["经营总览", "分域事件统计", "风险与整改项"]

        assert main(["monthly-report", "luneng", "--month", "bad-month"]) == 1
    finally:
        get_settings.cache_clear()
