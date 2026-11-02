"""OCR 提供方。统一接口，按配置切换：

- XiaomiOCR：调用 vLLM / SGLang 上的 Xiaomi-OCR-0（OpenAI 兼容 /chat/completions，图片以 data URI 传入），
  输出每页 Markdown（表格为 HTML、公式为 LaTeX）。
- MockOCR：不调用任何模型。按图片 SHA-256 查预置样例（packs/<pack>/fixtures/ocr/<sha256>.md），
  找不到就返回明确标注的占位文本。结果一律 data_mode=mock。
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Protocol

import httpx

from fos.config import OCRMode, Settings

# 部署时按 Xiaomi-OCR-0 官方 README 的推荐提示词核对
DEFAULT_PROMPT = (
    "请将图片中的全部内容识别并转换为 Markdown。表格使用 HTML 表格，公式使用 LaTeX，保留原有段落与编号。"
)


class OCRError(RuntimeError):
    pass


class OCRProvider(Protocol):
    data_mode: str

    async def recognize(self, image_png: bytes, *, page: int) -> str: ...

    async def aclose(self) -> None: ...


class MockOCR:
    data_mode = "mock"

    def __init__(self, fixtures_dirs: list[Path] | None = None):
        self._dirs = fixtures_dirs or []

    async def recognize(self, image_png: bytes, *, page: int) -> str:
        digest = hashlib.sha256(image_png).hexdigest()
        for d in self._dirs:
            f = d / f"{digest}.md"
            if f.exists():
                return f.read_text(encoding="utf-8")
        return f"> 【模拟识别】第 {page} 页为扫描件或图片。未部署 OCR 服务，此处不是真实识别结果。\n"

    async def aclose(self) -> None:
        return None


class XiaomiOCR:
    data_mode = "real"

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout_s: float = 120.0):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout_s)
        self._model = model

    async def recognize(self, image_png: bytes, *, page: int) -> str:
        url = "data:image/png;base64," + base64.b64encode(image_png).decode()
        body = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": url}},
                        {"type": "text", "text": DEFAULT_PROMPT},
                    ],
                }
            ],
        }
        try:
            r = await self._http.post("/chat/completions", json=body)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, KeyError, IndexError) as e:
            raise OCRError(f"第 {page} 页 OCR 失败：{e}") from e

    async def aclose(self) -> None:
        await self._http.aclose()


def make_ocr(s: Settings) -> OCRProvider:
    if s.ocr_mode is OCRMode.xiaomi:
        return XiaomiOCR(s.ocr_base_url, s.ocr_model, s.ocr_api_key, s.ocr_timeout_s)
    return MockOCR(sorted(s.packs_dir.glob("*/fixtures/ocr")))
