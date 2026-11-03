"""OTSL 表格标记转 HTML。

Xiaomi-OCR-0 的表格输出为 OTSL：每个单元格以标签开头，标签后的文字属于该单元格，<nl> 结束一行。

    <fcel> 有文字的单元格      <ecel> 空单元格
    <lcel> 与左边合并          <ucel> 与上面合并
    <xcel> 与左上同时合并      <nl>   行结束

只有 <fcel>/<ecel> 起一个新格；合并标签变成 HTML 的 colspan / rowspan。
整页 Markdown 里的表格可能包在 <otsl>…</otsl> 里，也可能是连续几行裸 OTSL，两种都处理。
"""

from __future__ import annotations

import html
import re

_TAGS = ("<fcel>", "<ecel>", "<lcel>", "<ucel>", "<xcel>")
_TAG_RE = re.compile(r"<(fcel|ecel|lcel|ucel|xcel)>")
_WRAPPED = re.compile(r"<otsl>(.*?)</otsl>", re.S)


def _rows(otsl: str) -> list[list[tuple[str, str]]]:
    out: list[list[tuple[str, str]]] = []
    for line in otsl.replace("\n", "").split("<nl>"):
        cells: list[tuple[str, str]] = []
        ms = list(_TAG_RE.finditer(line))
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(line)
            cells.append((m.group(1), line[m.end() : end].strip()))
        if cells:
            out.append(cells)
    return out


def otsl_to_html(otsl: str) -> str:
    rows = _rows(otsl)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    grid = [[*r, *[("ecel", "")] * (width - len(r))] for r in rows]
    n = len(grid)
    covered = [[False] * width for _ in range(n)]
    parts: list[str] = []
    for i in range(n):
        parts.append("<tr>")
        for j in range(width):
            tag, text = grid[i][j]
            if covered[i][j] or tag not in ("fcel", "ecel"):
                continue
            cs = 1
            while j + cs < width and grid[i][j + cs][0] in ("lcel", "xcel") and not covered[i][j + cs]:
                cs += 1
            rs = 1
            while i + rs < n and grid[i + rs][j][0] in ("ucel", "xcel"):
                rs += 1
            for di in range(rs):
                for dj in range(cs):
                    covered[i + di][j + dj] = True
            attrs = (f' rowspan="{rs}"' if rs > 1 else "") + (f' colspan="{cs}"' if cs > 1 else "")
            parts.append(f"<td{attrs}>{html.escape(text)}</td>")
        parts.append("</tr>")
    return "<table>" + "".join(parts) + "</table>"


def otsl_blocks_to_html(markdown: str) -> str:
    """把一页 Markdown 里的 OTSL 表格全部换成 HTML 表格，其余内容原样保留。"""
    if not any(t in markdown for t in _TAGS):
        return markdown
    markdown = _WRAPPED.sub(lambda m: otsl_to_html(m.group(1)), markdown)
    out: list[str] = []
    block: list[str] = []
    for line in markdown.split("\n"):
        if _TAG_RE.search(line) and "<table>" not in line:
            block.append(line)
            continue
        if block:
            out.append(otsl_to_html("\n".join(block)))
            block = []
        out.append(line)
    if block:
        out.append(otsl_to_html("\n".join(block)))
    return "\n".join(out)
