import hashlib

import httpx
import pytest

from fos.perception.ocr import MockOCR, OCRError, XiaomiOCR
from fos.perception.parser import ParseError, parse, sniff
from tests import samples

TEXT = (
    "1.1 Project: 2# roller kiln firing zone lining retrofit\n"
    "2.1 Normal working temperature 1250 C, max 1300 C"
)


async def test_pdf_text_layer_and_scanned_page():
    doc = await parse(samples.pdf([TEXT, None]), MockOCR())
    assert doc.format == "pdf"
    p1, p2 = doc.pages
    assert p1.method == "text_layer" and p1.data_mode == "real"
    assert "1250 C" in p1.markdown
    assert p2.method == "ocr_mock" and p2.data_mode == "mock"
    assert "模拟识别" in p2.markdown
    assert doc.data_mode == "mock"  # 有一页是模拟，整份就是模拟


async def test_pdf_with_only_text_is_real_even_in_mock_mode():
    doc = await parse(samples.pdf([TEXT, TEXT]), MockOCR())
    assert doc.data_mode == "real"
    assert all(p.method == "text_layer" for p in doc.pages)


async def test_mock_uses_fixture_by_image_hash(tmp_path):
    # 预置样例按渲染后 PNG 的哈希匹配；这里直接用图片文件验证
    img = samples.png()
    (tmp_path / f"{hashlib.sha256(img).hexdigest()}.md").write_text("送货单 No.0026517", encoding="utf-8")
    doc = await parse(img, MockOCR([tmp_path]))
    assert doc.format == "image"
    assert doc.pages[0].markdown == "送货单 No.0026517"
    assert doc.pages[0].data_mode == "mock"


async def test_docx_and_xlsx():
    d = await parse(samples.docx(), MockOCR())
    assert d.format == "docx" and d.data_mode == "real"
    md = d.pages[0].markdown
    assert md.startswith("# 2# 辊道窑")
    assert "<td>172.8 m²</td>" in md
    x = await parse(samples.xlsx(), MockOCR())
    assert [p.title for p in x.pages] == ["工程量清单", "说明"]
    assert "<td>100.8</td>" in x.pages[0].markdown


async def test_limits_and_bad_files():
    with pytest.raises(ParseError, match="超过上限"):
        await parse(samples.pdf([TEXT, TEXT, TEXT]), MockOCR(), max_pages=2)
    with pytest.raises(ParseError, match="不支持"):
        sniff(b"hello world")
    with pytest.raises(ParseError):
        await parse(b"%PDF-1.4 garbage", MockOCR())


async def test_xiaomi_provider_request_shape():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "| 温度 | 1250 |"}}]})

    ocr = XiaomiOCR("http://vllm/v1", "xiaomi-ocr-0", transport=httpx.MockTransport(handler))
    doc = await parse(samples.pdf([None]), ocr)
    await ocr.aclose()
    assert doc.pages[0].method == "ocr" and doc.data_mode == "real"
    content = seen["body"]["messages"][0]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert seen["body"]["model"] == "xiaomi-ocr-0" and seen["body"]["temperature"] == 0


async def test_xiaomi_provider_error():
    ocr = XiaomiOCR(
        "http://vllm/v1", "m", retry_delay_s=0, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    with pytest.raises(OCRError, match="第 1 页"):
        await parse(samples.png(), ocr)
    await ocr.aclose()
