"""OCR 提供方。统一接口，按 FOS_OCR_MODE 切换：

- xiaomi：Xiaomi-OCR-0（0.8B，Apache-2.0），部署在 vLLM / SGLang / Ollama 上，走 OpenAI 兼容
  /chat/completions。提示词和解码参数按官方 demo；表格输出为 OTSL，这里转成 HTML 表格。
- qwen_vl：阿里云百炼 Qwen-VL-OCR（compatible-mode 接口），适合还没有 GPU 的阶段先用云上模型。
- mock：不调用任何模型。按图片 SHA-256 查预置样例（packs/<pack>/fixtures/ocr/<sha256>.md，
  或按像素内容 px-<sha256>.md），
  找不到就返回明确标注的占位文本。结果一律 data_mode=mock。

真实提供方都是"图片 → 每页 Markdown（表格为 HTML）"，后面的抽取、界面、导出不区分来源。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from pathlib import Path
from typing import Any, Protocol

import httpx

from fos.config import OCRMode, Settings
from fos.perception.otsl import otsl_blocks_to_html

# Xiaomi-OCR-0 官方 demo（demo/server.py DOC_PROMPT）的整页解析提示词，保持原文不翻译
XIAOMI_PROMPT = (
    "Extract all information from the main body of the document image and represent it in markdown format, "
    "ignoring headers and footers. Tables should be expressed in OTSL format, "
    "formulas in the document should be represented using LATEX format, "
    "and the parsing should be organized according to the reading order."
)
QWEN_PROMPT = (
    "请识别图片中的全部文字，按阅读顺序输出 Markdown。表格使用 HTML <table> 输出并保留合并单元格，"
    "公式使用 LaTeX，保留原有标题、编号和单位。只输出识别结果，不要解释，不要补充图片里没有的内容。"
)

DEFAULTS: dict[OCRMode, dict[str, str]] = {
    OCRMode.xiaomi: {"base_url": "http://localhost:8001/v1", "model": "xiaomi-ocr-0"},
    OCRMode.qwen_vl: {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-ocr-latest",
    },
}


class OCRError(RuntimeError):
    pass


class OCRProvider(Protocol):
    data_mode: str
    name: str

    async def recognize(self, image_png: bytes, *, page: int) -> str: ...

    async def aclose(self) -> None: ...


class MockOCR:
    data_mode = "mock"
    name = "mock"

    def __init__(self, fixtures_dirs: list[Path] | None = None):
        self._dirs = fixtures_dirs or []

    async def recognize(self, image_png: bytes, *, page: int) -> str:
        # 先按 PNG 字节，再按像素内容（不受 PNG 编码器版本影响，样例照片用这个）
        digests = [hashlib.sha256(image_png).hexdigest(), pixel_digest(image_png)]
        for d in self._dirs:
            for digest in digests:
                f = d / f"{digest}.md"
                if digest and f.exists():
                    return f.read_text(encoding="utf-8")
        return f"> 【模拟识别】第 {page} 页为扫描件或图片。未部署 OCR 服务，此处不是真实识别结果。\n"

    async def aclose(self) -> None:
        return None


def pixel_digest(image_png: bytes) -> str:
    """图片像素内容的 SHA-256（RGB + 尺寸）。读不了的图片返回空串。"""
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(image_png)).convert("RGB")
    except (UnidentifiedImageError, OSError):
        return ""
    h = hashlib.sha256(f"{img.width}x{img.height}:".encode())
    h.update(img.tobytes())
    return "px-" + h.hexdigest()


class OpenAICompatOCR:
    """任何 OpenAI 兼容的视觉模型接口：图片以 data URI 传入，返回整页 Markdown。"""

    data_mode = "real"
    name = "openai_compat"
    prompt = QWEN_PROMPT

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_s: float = 120.0,
        *,
        max_tokens: int = 4096,
        retries: int = 2,
        retry_delay_s: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,  # 测试注入
    ):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s, transport=transport
        )
        self._model = model
        self._max_tokens = max_tokens
        self._retries = retries
        self._retry_delay_s = retry_delay_s

    def _body(self, data_uri: str) -> dict[str, Any]:
        return {
            "model": self._model,
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": self.prompt},
                    ],
                }
            ],
        }

    def _post(self, content: str) -> str:
        return content

    async def recognize(self, image_png: bytes, *, page: int) -> str:
        body = self._body("data:image/png;base64," + base64.b64encode(image_png).decode())
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                r = await self._http.post("/chat/completions", json=body)
                if r.status_code < 500 and r.status_code != 429:
                    r.raise_for_status()
                    content = r.json()["choices"][0]["message"]["content"] or ""
                    if isinstance(content, list):  # 个别服务返回分段内容
                        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                    return self._post(content)
                last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            except httpx.HTTPStatusError as e:  # 4xx：请求本身有问题，重试没用
                raise OCRError(f"第 {page} 页 OCR 失败：{e}") from e
            except (httpx.TransportError, KeyError, IndexError, ValueError) as e:
                last = e
            if attempt < self._retries:
                await asyncio.sleep(self._retry_delay_s * (attempt + 1))
        raise OCRError(f"第 {page} 页 OCR 失败：{last}")

    async def aclose(self) -> None:
        await self._http.aclose()


class XiaomiOCR(OpenAICompatOCR):
    name = "xiaomi"
    prompt = XIAOMI_PROMPT

    def _body(self, data_uri: str) -> dict[str, Any]:
        body = super()._body(data_uri)
        # 官方 demo 的解码参数；关闭思考模式（Qwen3.5 底座）
        body.update(top_p=0.8, presence_penalty=0.0, repetition_penalty=1.0)
        body["chat_template_kwargs"] = {"enable_thinking": False}
        return body

    def _post(self, content: str) -> str:
        return otsl_blocks_to_html(content)


class QwenVLOCR(OpenAICompatOCR):
    name = "qwen_vl"
    prompt = QWEN_PROMPT


def make_ocr(s: Settings) -> OCRProvider:
    if s.ocr_mode is OCRMode.mock:
        return MockOCR(sorted(s.packs_dir.glob("*/fixtures/ocr")))
    d = DEFAULTS[s.ocr_mode]
    cls = XiaomiOCR if s.ocr_mode is OCRMode.xiaomi else QwenVLOCR
    return cls(
        s.ocr_base_url or d["base_url"],
        s.ocr_model or d["model"],
        s.ocr_api_key,
        s.ocr_timeout_s,
        max_tokens=s.ocr_max_tokens,
        retries=s.ocr_retries,
    )
