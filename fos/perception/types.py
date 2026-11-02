from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

PageMethod = Literal["text_layer", "ocr", "ocr_mock", "docx", "xlsx"]
DataMode = Literal["real", "mock"]
DocFormat = Literal["pdf", "image", "docx", "xlsx"]


class PageResult(BaseModel):
    page: int  # 从 1 开始；Excel 为工作表序号
    method: PageMethod
    markdown: str
    data_mode: DataMode
    title: str | None = None  # Excel 工作表名等
    chars: int = 0


class ParsedDocument(BaseModel):
    format: DocFormat
    pages: list[PageResult]

    @property
    def data_mode(self) -> DataMode:
        # 只要有一页是模拟识别，整份文档就是模拟数据：不允许静默混用（方案 4.7）
        return "mock" if any(p.data_mode == "mock" for p in self.pages) else "real"
