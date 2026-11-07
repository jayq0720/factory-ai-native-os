"""规则抽取：按 Schema 里的叫法、取值和正则在逐页原文里找字段，每个结果都带页码和原文片段。

规则抽取不调用模型、结果确定，有文字层的 PDF / Word / Excel 在没有任何模型服务时也能直接用。
模型抽取（llm.py）在它的基础上补充，两者的候选值在 merge() 里合并。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from fos.extraction.schema import ExtractionSchema, FieldDef

NUM = r"(-?\d+(?:\.\d+)?)"
TEMP_RE = re.compile(NUM + r"\s*(?:℃|°\s*C|摄氏度|C(?![a-zA-Z])|度)", re.I)
UNIT_ALIASES = {
    "m2": "m²",
    "㎡": "m²",
    "平方米": "m²",
    "平米": "m²",
    "m3": "m³",
    "立方米": "m³",
    "方": "m³",
    "吨": "t",
    "公斤": "kg",
    "毫米": "mm",
    "厘米": "cm",
    "米": "m",
    "W/m2": "W/m²",
    "w/m2": "W/m²",
    "w/m²": "W/m²",
}
NUMBER_RE = re.compile(
    NUM
    + r"\s*(W/m²|W/m2|w/m2|m²|m2|㎡|平方米|平米|m³|m3|立方米|mm|毫米|cm|厘米|kg|公斤|吨"
    + r"|t(?![a-zA-Z])|%|米|m(?![a-zA-Z²³]))?"
)
MONEY_RE = re.compile(r"[￥¥]?\s*" + NUM + r"\s*(万元|元|万)")
DATE_RE = re.compile(
    r"\d{4}\s*[年\-/.]\s*\d{1,2}\s*[月\-/.]\s*\d{1,2}\s*日?"
    r"(?:\s*[（(]?[上下]午[)）]?)?(?:\s*\d{1,2}\s*[:：时点]\s*(?:\d{1,2}\s*分?)?)?"
)
SEP_AFTER_LABEL = re.compile(r"^\s*(?:[（(][^）)]{0,12}[）)])?\s*(?:[:：=＝]|为|是)?\s*")
CUT_SOFT = re.compile(r"[；;。\n]")  # 文字、枚举值截到这里
CUT_HARD = re.compile(r"[，,；;。、\n]")  # 数值截到这里，避免取到下一个指标
TAG_RE = re.compile(r"<[^>]+>")
MD_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|[>*\-+]\s+|\d+[.、)]\s+(?=\D))?")

MAX_SOURCES = 30
MAX_LIST = 200


@dataclass
class Segment:
    page: int
    text: str  # 原文一行（表格为一行单元格用 " | " 连起来）
    cells: list[str] | None = None
    table: int | None = None  # 同一张表的行有相同编号
    row: int = 0


@dataclass
class Source:
    page: int
    quote: str
    highlight: list[str] = field(default_factory=list)

    def dump(self) -> dict:
        return {"page": self.page, "quote": self.quote, "highlight": self.highlight or [self.quote]}


@dataclass
class Candidate:
    value: str
    unit: str | None
    sources: list[Source]
    confidence: float
    method: str  # rule / llm

    def dump(self) -> dict:
        return {
            "value": self.value,
            "unit": self.unit,
            "confidence": round(self.confidence, 2),
            "method": self.method,
            "sources": [s.dump() for s in self.sources[:MAX_SOURCES]],
        }


# ---------------- 原文切分 ----------------

_TABLE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)


def _cell(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", s))).strip()


def segments(page: int, markdown: str) -> list[Segment]:
    out: list[Segment] = []
    tables: list[str] = []

    def stash(m: re.Match) -> str:
        tables.append(m.group(1))
        return f"\n\x00T{len(tables) - 1}\x00\n"

    body = _TABLE.sub(stash, markdown)
    tno = 0
    pipe_rows: list[list[str]] = []

    def flush_pipe() -> None:
        nonlocal tno, pipe_rows
        if pipe_rows:
            for r, cells in enumerate(pipe_rows):
                out.append(Segment(page, " | ".join(cells), cells, table=tno, row=r))
            tno += 1
            pipe_rows = []

    for raw in body.split("\n"):
        line = raw.strip()
        m = re.fullmatch(r"\x00T(\d+)\x00", line)
        if m:
            flush_pipe()
            for r, tr in enumerate(_TR.findall(tables[int(m.group(1))])):
                cells = [_cell(c) for c in _TD.findall(tr)]
                if any(cells):
                    out.append(Segment(page, " | ".join(cells), cells, table=tno, row=r))
            tno += 1
            continue
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                pipe_rows.append(cells)
            continue
        flush_pipe()
        line = (
            MD_PREFIX.sub("", line, count=1).strip() if line.startswith(("#", ">", "*", "-", "+")) else line
        )
        if line:
            out.append(Segment(page, line))
    flush_pipe()
    return out


# ---------------- 取值 ----------------


def _unit(u: str | None, default: str | None) -> str | None:
    if not u:
        return default
    return UNIT_ALIASES.get(u, u)


def parse_value(f: FieldDef, region: str) -> tuple[str, str | None, str] | None:
    """从叫法后面的一段原文里取值。返回 (值, 单位, 命中的原文)。"""
    if f.type == "temperature":
        m = TEMP_RE.search(CUT_HARD.split(region, 1)[0])
        return (m.group(1), f.unit or "℃", m.group(0)) if m else None
    if f.type == "number":
        part = CUT_HARD.split(region, 1)[0]
        m = NUMBER_RE.search(part)
        return (m.group(1), _unit(m.group(2), f.unit), m.group(0).strip()) if m else None
    if f.type == "money":
        m = MONEY_RE.search(CUT_SOFT.split(region, 1)[0])
        return (m.group(1), m.group(2), m.group(0).strip()) if m else None
    if f.type == "date":
        m = DATE_RE.search(CUT_SOFT.split(region, 1)[0])
        return (re.sub(r"\s+", "", m.group(0)), None, m.group(0)) if m else None
    if f.type == "enum":
        hits = _enum_hits(f, CUT_SOFT.split(region, 1)[0][:80])
        if not hits:
            return None
        return ("、".join(hits) if f.multi else hits[0], None, hits[0])
    # text / list
    v = CUT_SOFT.split(region, 1)[0].strip().strip("，,：:").strip()
    v = v[:120]
    # 值要以文字开头：排除"工作层 + 隔热层"这种叫法后面紧跟连接符的情况
    if len(v) < 2 or not re.match(r"[\w一-鿿（(《“\"]", v):
        return None
    return v, None, v


def _enum_hits(f: FieldDef, text: str) -> list[str]:
    """按出现顺序返回命中的取值；长的优先，避免"弱还原"里再算一次"还原"。"""
    taken: list[tuple[int, int, str]] = []
    for v in sorted(f.enum, key=len, reverse=True):
        for m in re.finditer(re.escape(v), text):
            if all(m.end() <= a or m.start() >= b for a, b, _ in taken):
                taken.append((m.start(), m.end(), v))
    return list(dict.fromkeys(v for _, _, v in sorted(taken)))


def _label_positions(f: FieldDef, text: str) -> list[tuple[int, str, int]]:
    """(位置, 叫法, 叫法序号)，排除 exclude_before 的情况。"""
    low = text.lower()
    out: list[tuple[int, str, int]] = []
    for li, label in enumerate(f.labels):
        lab = label.lower()
        start = 0
        while (i := low.find(lab, start)) >= 0:
            start = i + len(lab)
            before = low[max(0, i - 4) : i]
            if any(x.lower() in before for x in f.exclude_before):
                continue
            # 英文叫法要整词匹配
            if lab.isascii() and (
                (i > 0 and low[i - 1].isalpha()) or (start < len(low) and low[start].isalpha())
            ):
                continue
            # 被更长的叫法覆盖的位置不重复计
            if any(p <= i < p + len(lb) for p, lb, _ in out):
                continue
            out.append((i, label, li))
    return out


def _label_conf(f: FieldDef, li: int, label: str) -> float:
    base = 0.85 if li == 0 else 0.75
    return base - 0.1 if len(label) <= 2 else base


def _quote_window(text: str, start: int, end: int, width: int = 160) -> str:
    if len(text) <= width:
        return text
    a = max(0, start - 30)
    return text[a : max(end, a + width)]


def _extract_line(f: FieldDef, seg: Segment) -> list[tuple[str, str | None, Source, float]]:
    out = []
    for pos, label, li in _label_positions(f, seg.text):
        region = seg.text[pos + len(label) :]
        if f.type in ("text", "list"):
            sep = SEP_AFTER_LABEL.match(region)
            if not sep or sep.end() == 0:
                # 文字字段一般要求叫法后面有冒号、空格等分隔；叫法够长时取它所在的分句
                # （"升温速率不大于 150℃/h"），可信度低一些
                if len(label) < 3 or not region[:1] or not re.match(r"[\w\u4e00-\u9fff]", region[:1]):
                    continue
                clause = CUT_SOFT.split(seg.text[pos:], 1)[0].strip()
                if len(clause) > len(label) + 1:
                    quote = _quote_window(seg.text, pos, pos + len(clause))
                    out.append((clause[:120], None, Source(seg.page, quote, [quote]), 0.6))
                continue
        label_unit = _unit_in_label_cell(region[:14]) if region.lstrip().startswith(("(", "（")) else None
        region = SEP_AFTER_LABEL.sub("", region, count=1)
        conf = _label_conf(f, li, label)
        if f.type == "enum" and f.multi:  # 多值枚举：每个取值一条，合并时再连起来
            for v in _enum_hits(f, CUT_SOFT.split(region, 1)[0][:80]):
                quote = _quote_window(seg.text, pos, pos + len(label))
                out.append((v, None, Source(seg.page, quote, [quote]), conf))
            continue
        pv = parse_value(f, region)
        if pv is None and label_unit and f.type in ("number", "temperature"):
            # 叫法自带单位：“工作温度（℃） 1280”
            m = re.match(NUM, CUT_HARD.split(region, 1)[0].strip())
            pv = (m.group(1), label_unit, m.group(0)) if m else None
        if pv is None:
            continue
        value, unit, hit = pv
        end = seg.text.find(hit, pos) + len(hit) if hit in seg.text[pos:] else pos + len(label)
        quote = _quote_window(seg.text, pos, end)
        out.append((value, unit, Source(seg.page, quote, [quote]), conf))
    return out


def _cell_has_label(f: FieldDef, cell: str) -> tuple[str, int] | None:
    hits = _label_positions(f, cell)
    if not hits:
        return None
    # 单元格是"叫法"本身（可带括号单位），不是一句长话
    if len(cell) > max(len(h[1]) for h in hits) + 12:
        return None
    _, label, li = hits[0]
    return label, li


def _unit_in_label_cell(cell: str) -> str | None:
    m = re.search(r"[（(]\s*([^）)]{1,8})\s*[）)]", cell)
    return _unit(m.group(1).strip(), None) if m else None


def _extract_table(
    f: FieldDef, rows: list[Segment]
) -> tuple[list[tuple[str, str | None, Source, float]], set[int]]:
    """表格：叫法在某格里，值在右边的格（行式）或下面各行同一列（列式，工程量清单）。"""
    out = []
    used: set[int] = set()
    for ri, seg in enumerate(rows):
        cells = seg.cells or []
        for ci, cell in enumerate(cells):
            hit = _cell_has_label(f, cell)
            if hit is None:
                continue
            label, li = hit
            label_unit = _unit_in_label_cell(cell)
            right = [c for c in cells[ci + 1 :] if c]
            pv = None
            if right:
                region = " ".join(right[:2])
                pv = parse_value(f, region)
                if pv is None and label_unit and f.type in ("number", "temperature"):
                    m = re.match(NUM, right[0].strip())  # 叫法格带单位、值格是裸数字
                    pv = (m.group(1), label_unit, m.group(0)) if m else None
            if pv is not None:
                value, unit, _ = pv
                if f.type in ("number", "temperature") and label_unit and unit == f.unit:
                    unit = label_unit
                out.append(
                    (
                        value,
                        unit,
                        Source(seg.page, seg.text, [cell, right[0]]),
                        _label_conf(f, li, label) + 0.05,
                    )
                )
                used.add(ri)
                continue
            if f.type not in ("number", "temperature", "money"):
                continue
            # 列式：表头是叫法，下面每行同一列是值；"单位"列优先
            unit_col = next((k for k, c in enumerate(cells) if c in ("单位", "计量单位")), None)
            for rj in range(ri + 1, min(len(rows), ri + 200)):
                rc = rows[rj].cells or []
                if ci >= len(rc) or not rc[ci]:
                    continue
                pv = parse_value(f, rc[ci])
                if pv is None:
                    continue
                value, unit, _ = pv
                if unit_col is not None and unit_col < len(rc) and rc[unit_col]:
                    unit = _unit(rc[unit_col], unit)
                elif label_unit:
                    unit = label_unit
                out.append((value, unit, Source(rows[rj].page, rows[rj].text, [rc[ci]]), 0.8))
                used.add(rj)
    return out, used


def _extract_patterns(f: FieldDef, segs: list[Segment]) -> list[tuple[str, str | None, Source, float]]:
    out = []
    for p in f.patterns:
        rx = re.compile(p)
        for seg in segs:
            for m in rx.finditer(seg.text):
                v = re.sub(r"\s+", " ", m.group(0)).strip()
                if len(v) < 2:
                    continue
                src = Source(seg.page, _quote_window(seg.text, m.start(), m.end()), [m.group(0).strip()])
                out.append((v, None, src, 0.8))
                if len(out) >= MAX_LIST:
                    return out
    return out


def _extract_scan(f: FieldDef, segs: list[Segment]) -> list[tuple[str, str | None, Source, float]]:
    out = []
    for seg in segs:
        for v in _enum_hits(f, seg.text):
            out.append((v, None, Source(seg.page, _quote_window(seg.text, 0, 0), [v]), 0.5))
    return out


def extract_field(f: FieldDef, segs: list[Segment]) -> list[Candidate]:
    raw: list[tuple[str, str | None, Source, float]] = []
    if f.patterns:
        raw += _extract_patterns(f, segs)
    if f.labels and not (f.type == "list" and f.patterns):
        tables: dict[tuple[int, int], list[Segment]] = {}
        for s in segs:
            if s.table is not None:
                tables.setdefault((s.page, s.table), []).append(s)
            else:
                raw += _extract_line(f, s)
        for rows in tables.values():
            found, used = _extract_table(f, rows)
            raw += found
            # 表格里叫法和值在同一个单元格的（"工作温度：1250℃"）按行处理
            for ri, s in enumerate(rows):
                if ri not in used:
                    for c in s.cells or []:
                        raw += _extract_line(f, Segment(s.page, c))
    if f.type == "enum" and f.scan_all:
        raw += _extract_scan(f, segs)
    return group(raw, method="rule")


def group(raw: list[tuple[str, str | None, Source, float]], *, method: str) -> list[Candidate]:
    """同值同单位的命中合并成一个候选，来源累加、可信度取最高。"""
    by: dict[tuple[str, str | None], Candidate] = {}
    for value, unit, src, conf in raw:
        k = (value, unit)
        c = by.get(k)
        if c is None:
            by[k] = Candidate(value, unit, [src], conf, method)
        else:
            if not any(s.page == src.page and s.quote == src.quote for s in c.sources):
                c.sources.append(src)
            c.confidence = max(c.confidence, conf)
    return list(by.values())


def extract_rules(schema: ExtractionSchema, pages: list[tuple[int, str]]) -> dict[str, list[Candidate]]:
    segs = [s for page, md in pages for s in segments(page, md)]
    return {f.key: extract_field(f, segs) for f in schema.fields}
