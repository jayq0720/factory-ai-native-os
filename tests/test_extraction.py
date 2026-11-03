"""字段抽取：Schema、规则抽取（带页码原文）、冲突与缺失、模型抽取的防编造校验、OTSL 表格、OCR 提供方。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from fos.config import LLMMode, OCRMode, Settings
from fos.extraction.llm import extract_llm
from fos.extraction.merge import combine, resolve
from fos.extraction.rules import extract_rules, segments
from fos.extraction.schema import ExtractionSchema, FieldDef
from fos.gateway.llm import ChatResult
from fos.perception.ocr import QwenVLOCR, XiaomiOCR, make_ocr
from fos.perception.otsl import otsl_blocks_to_html, otsl_to_html
from fos.perception.parser import parse
from fos.quickstart import sample_boq_xlsx, sample_tech_docx, sample_tender_docx
from tests import samples

REPO = Path(__file__).resolve().parents[1]


def _schema(name: str) -> ExtractionSchema:
    raw = yaml.safe_load((REPO / f"packs/refractory/extraction/{name}.yaml").read_text(encoding="utf-8"))
    return ExtractionSchema.model_validate(raw)


async def _fields(name: str, data: bytes) -> dict:
    from fos.perception.ocr import MockOCR

    doc = await parse(data, MockOCR())
    pages = [(p.page, p.markdown) for p in doc.pages]
    schema = _schema(name)
    rule = extract_rules(schema, pages)
    modes = {p.page: p.data_mode for p in doc.pages}
    return {f.key: resolve(f, rule[f.key], modes, doc.data_mode) for f in schema.fields}


# ---------------- Schema ----------------


def test_pack_schemas_load_and_validate():
    tech, tender = _schema("tech"), _schema("tender")
    assert "tech" in tech.kinds and "tender" in tender.kinds
    assert tech.field("normal_temp").type == "temperature"
    assert tech.groups[:3] == ["项目", "设备与部位", "工况"]
    with pytest.raises(ValueError, match="enum"):
        FieldDef(key="x", name="x", group="g", type="enum", labels=["x"])
    with pytest.raises(ValueError, match="至少"):
        FieldDef(key="x", name="x", group="g")
    with pytest.raises(ValueError, match="重复"):
        ExtractionSchema(id="s", name="s", kinds=["tech"], fields=[tech.fields[0], tech.fields[0]])


# ---------------- 规则抽取 ----------------


async def test_tech_document_fields_with_citations():
    r = await _fields("tech", sample_tech_docx())
    assert (r["normal_temp"].value, r["normal_temp"].unit) == ("1250", "℃")
    assert r["max_temp"].value == "1300"  # "最高温度"不会被当成正常工作温度
    assert r["shell_temp"].value == "60"
    assert r["atmosphere"].value == "氧化、弱还原"
    assert r["furnace_type"].value == "辊道窑"
    assert r["customer_name"].value == "安徽某陶瓷有限公司"
    assert r["hot_face"].value == "JM-26 莫来石轻质砖 230 mm"  # 不会取到"工作层 + 隔热层"
    assert r["total_thickness"].value == "350" and r["total_thickness"].unit == "mm"
    assert r["standards"].value == "GB 50211-2014；GB/T 3994-2013"
    assert (r["area"].value, r["area"].unit) == ("172.8；100.8；4.3", "m²")  # 表格列式
    assert r["tax_rate"].value == "13" and r["tax_rate"].unit == "%"
    assert r["thermal_cycle"].value.startswith("升温速率不大于 150℃/h")
    # 每个抽到的字段都有页码和能在原文里找到的片段
    src = r["normal_temp"].sources[0]
    assert src["page"] == 1 and "正常工作温度 1250℃" in src["quote"]
    # 原文没有的字段标缺失，不编造
    assert r["heat_loss"].status == "missing" and r["heat_loss"].value is None
    assert all(x.data_mode == "real" for x in r.values())


async def test_tender_key_info_and_clauses():
    r = await _fields("tender", sample_tender_docx())
    assert r["tender_no"].value == "ZB-2026-0458"
    assert (r["budget"].value, r["budget"].unit) == ("186.5", "万元")
    assert r["bid_deadline"].value == "2026年12月18日09:30"
    assert not r["bid_deadline"].conflict  # "澄清截止时间"不算投标截止
    assert r["clarification_deadline"].value == "2026年12月8日17:00"
    assert r["evaluation_method"].value == "综合评分法"
    assert r["phone"].value == "0564-3312345"
    starred = r["starred_clauses"]
    assert "★ 投标产品分类温度不得低于 1400℃。" in starred.value
    assert len(starred.sources) >= 3
    assert "无效投标" in r["disqualify_clauses"].value


async def test_boq_xlsx_and_label_unit_in_cell():
    r = await _fields("tech", sample_boq_xlsx())
    assert r["area"].value == "172.8；100.8；4.3"  # 同值合并、保持出现顺序
    assert (r["normal_temp"].value, r["normal_temp"].unit) == ("1250", "℃")  # "工作温度（℃）" | 1250
    assert r["normal_temp"].sources[0]["page"] == 2


def test_conflict_between_pages_is_flagged():
    schema = _schema("tech")
    pages = [(1, "2.1 正常工作温度 1250℃，最高温度 1300℃"), (3, "工作温度：1280℃")]
    rule = extract_rules(schema, pages)
    r = resolve(schema.field("normal_temp"), rule["normal_temp"], {1: "real", 3: "real"}, "real")
    assert r.value == "1250" and r.conflict
    assert {c["value"] for c in r.candidates} == {"1250", "1280"}


def test_mock_page_marks_field_mock():
    schema = _schema("tech")
    rule = extract_rules(schema, [(1, "工作温度：1250℃")])
    r = resolve(schema.field("normal_temp"), rule["normal_temp"], {1: "mock"}, "mock")
    assert r.data_mode == "mock"


def test_segments_from_html_and_pipe_tables():
    segs = segments(
        1, "# 标题\n<table><tr><td>部位</td><td>面积</td></tr></table>\n| a | b |\n|---|---|\n| 1 | 2 |"
    )
    assert [s.text for s in segs] == ["标题", "部位 | 面积", "a | b", "1 | 2"]
    assert segs[1].cells == ["部位", "面积"] and segs[2].table != segs[1].table


# ---------------- 模型抽取：防编造 ----------------


class FakeLLM:
    mode = LLMMode.litellm

    def __init__(self, items):
        self.items = items
        self.calls = 0

    async def chat(self, messages, *, model, tenant_id, json_mode=False):
        self.calls += 1
        assert json_mode and model == "fos-chat"
        return ChatResult(model=model, content=json.dumps({"fields": self.items}), data_mode="real")


async def test_llm_results_must_quote_the_page():
    schema = _schema("tech")
    pages = [(1, "炉内工作温度一般在 1250℃ 左右，偶尔到 1320℃。\n客户：某陶瓷厂")]
    llm = FakeLLM(
        [
            {"key": "normal_temp", "value": "1250", "unit": "℃", "page": 1, "quote": "工作温度一般在 1250℃"},
            {"key": "max_temp", "value": "1320", "unit": "℃", "page": 1, "quote": "偶尔到 1320℃"},
            # 编造：原文里没有这句
            {"key": "shell_temp", "value": "60", "unit": "℃", "page": 1, "quote": "外壁温度 60℃"},
            # 页码不对
            {"key": "customer_name", "value": "某陶瓷厂", "page": 2, "quote": "客户：某陶瓷厂"},
            # 值不在片段里
            {"key": "atmosphere", "value": "还原", "page": 1, "quote": "工作温度一般在"},
            {"key": "no_such_field", "value": "x", "page": 1, "quote": "客户"},
        ]
    )
    out = await extract_llm(schema, pages, llm, tenant_id="luneng")
    assert [c.value for c in out["normal_temp"]] == ["1250"]
    assert [c.value for c in out["max_temp"]] == ["1320"]
    assert out["shell_temp"] == [] and out["customer_name"] == [] and out["atmosphere"] == []
    # 规则也找到 1250：合并后可信度更高、方式标为 规则+模型
    rule = extract_rules(schema, pages)
    merged = combine(rule["normal_temp"], out["normal_temp"])
    r = resolve(schema.field("normal_temp"), merged, {1: "real"}, "real")
    assert r.value == "1250" and r.method == "rule+llm" and r.confidence > 0.85


async def test_llm_failure_falls_back_to_rules():
    class Broken(FakeLLM):
        async def chat(self, *a, **k):
            from fos.gateway.llm import LLMError

            raise LLMError("网关不可用")

    out = await extract_llm(_schema("tech"), [(1, "工作温度 1250℃")], Broken([]), tenant_id="luneng")
    assert all(v == [] for v in out.values())


# ---------------- OTSL 与 OCR 提供方 ----------------


def test_otsl_to_html_spans():
    html = otsl_to_html(
        "<fcel>部位<fcel>面积<lcel><nl><fcel>炉墙<fcel>172.8<fcel>m²<nl><ucel><fcel>100.8<fcel>m²<nl>"
    )
    assert html.startswith('<table><tr><td>部位</td><td colspan="2">面积</td></tr>')
    assert '<td rowspan="2">炉墙</td>' in html
    page = "## 工程量\n<otsl><fcel>a<fcel>b<nl></otsl>\n说明\n<fcel>x<ecel><nl>"
    out = otsl_blocks_to_html(page)
    assert "<table><tr><td>a</td><td>b</td></tr></table>" in out
    assert "<table><tr><td>x</td><td></td></tr></table>" in out and "说明" in out
    assert otsl_blocks_to_html("没有表格") == "没有表格"


async def test_xiaomi_request_follows_official_demo_and_converts_otsl():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "<fcel>温度<fcel>1250℃<nl>"}}]})

    ocr = XiaomiOCR("http://vllm/v1", "xiaomi-ocr-0", retries=0, transport=httpx.MockTransport(handler))
    doc = await parse(samples.pdf([None]), ocr)
    await ocr.aclose()
    assert doc.pages[0].markdown == "<table><tr><td>温度</td><td>1250℃</td></tr></table>"
    b = seen["body"]
    assert b["chat_template_kwargs"] == {"enable_thinking": False} and b["top_p"] == 0.8
    assert "OTSL" in b["messages"][0]["content"][1]["text"]


async def test_ocr_retries_transient_errors_and_runs_pages_concurrently():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "识别结果"}}]})

    ocr = QwenVLOCR(
        "https://dashscope/compatible-mode/v1",
        "qwen-vl-ocr-latest",
        "k",
        retry_delay_s=0,
        transport=httpx.MockTransport(handler),
    )
    progress = []

    async def on_progress(done, total):
        progress.append((done, total))

    doc = await parse(samples.pdf([None, None, None]), ocr, concurrency=2, on_progress=on_progress)
    await ocr.aclose()
    assert [p.markdown for p in doc.pages] == ["识别结果"] * 3 and doc.data_mode == "real"
    assert calls["n"] == 4 and progress[0] == (0, 3) and progress[-1] == (3, 3)


def test_make_ocr_defaults_per_mode(tmp_path):
    s = Settings(ocr_mode=OCRMode.qwen_vl, ocr_api_key="sk-test", packs_dir=tmp_path)
    o = make_ocr(s)
    assert isinstance(o, QwenVLOCR) and o._model == "qwen-vl-ocr-latest"
    assert str(o._http.base_url).startswith("https://dashscope.aliyuncs.com/compatible-mode/v1")
    x = make_ocr(Settings(ocr_mode=OCRMode.xiaomi, packs_dir=tmp_path))
    assert isinstance(x, XiaomiOCR) and x._model == "xiaomi-ocr-0"
