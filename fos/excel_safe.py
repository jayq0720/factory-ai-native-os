"""Excel / Word 导出文本清洗：防止公式注入（CSV/DDE）与非法 XML 控制字符导致导出崩溃。"""

from __future__ import annotations

import re
from typing import Any

# XML 1.0 禁止的控制字符（保留 \t \n \r）；PDF 抽取常带 \x0c（换页符）等
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def xml_safe(text: str) -> str:
    """剔除 XML 1.0 非法控制字符，避免 openpyxl / python-docx 抛 IllegalCharacterError。"""
    return _ILLEGAL_XML.sub("", text)


def excel_cell(value: Any) -> Any:
    """写入 Excel 单元格前的安全清洗：保持数字/布尔原类型，字符串防公式注入并清非法字符。"""
    if not isinstance(value, str):
        return value
    s = xml_safe(value)
    stripped = s.lstrip(" ")
    if stripped and stripped[0] in _FORMULA_PREFIX:
        return "'" + s
    return s


def excel_row(values: list[Any] | tuple[Any, ...]) -> list[Any]:
    return [excel_cell(v) for v in values]
