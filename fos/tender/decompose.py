"""招标文件拆解的确定性部分（纯函数，零模型成本，方便测试与重放）。

- 结构识别：按标题行（Markdown #、“第 X 章”）和 tender.yaml 的章节关键词归类；
- 条款拆解：逐行（表格逐行）切成条款，按 ★ / ▲ / “必须”“不得” 分级，按关键词或章节分类；
  ★ 条款只认原文里的标记，宁多勿漏（方案 7 章指标：★ 召回率 ≥ 95%）；
- 评分点：评标办法章节里带“N 分”的句子；
- 资料清单：条款里提到的营业执照、检测报告、业绩……；
- 偏离：从条款里读出“不低于 1400℃”“不少于 12 个月”这类指标，和我方能力比较（数字由代码比较，规则 3）。

模型不参与这里的任何判断；以后接模型只做补充（例如给没有标记的条款建议类别），不改原文与页码。
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from typing import Any

from fos.tender.config import Capability, TenderConfig

_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
_CHAPTER = re.compile(r"^\s*(第[一二三四五六七八九十百零〇\d]+[章篇部分卷节])\s*(.{0,30})$")
_NUMBER = re.compile(
    r"^\s*(?:[★▲]\s*)?((?:\d+\.)+\d+|\d+[\.、)）](?!\d)|[（(][一二三四五六七八九十\d]+[)）]|[一二三四五六七八九十]+、)\s*"
)
_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_TAG = re.compile(r"<[^>]+>")
# “应”“须”误伤的常见词：供应商、响应、须知……
_NOISE = re.compile(r"供应|响应|相应|对应|应答|适应|反应|理应|须知|必须知")
_POINTS = re.compile(r"(\d+(?:\.\d+)?)\s*分(?![钟之类别析数项包])")
_CAP_WORD = re.compile(r"最多|最高|满分|封顶|上限|不超过")
_CN_NUM = {
    "半": 0.5,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass
class Line:
    page: int
    text: str
    section: str | None  # 章节类别 id（tender.yaml sections），没识别出来为 None
    heading: bool = False


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", s))).strip()


def _table_lines(block: str) -> list[str]:
    out = []
    for row in _ROW.findall(block):
        cells = [_clean(c) for c in _CELL.findall(row)]
        cells = [c for c in cells if c]
        if len(cells) < 2 or ("序号" in cells and not any(ch.isdigit() for ch in "".join(cells))):
            continue  # 表头
        if re.fullmatch(r"\d+", cells[0]):
            cells = cells[1:]
        out.append(f"{cells[0]}：{' '.join(cells[1:])}" if len(cells) > 1 else cells[0])
    return out


def _raw_lines(md: str) -> list[tuple[str, bool]]:
    """一页 Markdown → [(文字, 是否标题)]。HTML 表格逐行展开；Markdown 管道表格同样处理。"""
    out: list[tuple[str, bool]] = []
    for block in re.split(r"(<table>.*?</table>)", md, flags=re.S):
        if block.startswith("<table>"):
            out += [(t, False) for t in _table_lines(block)]
            continue
        for raw in block.split("\n"):
            s = raw.strip()
            if not s or re.fullmatch(r"\|?\s*:?-{2,}.*", s):
                continue
            if s.startswith("|") and s.endswith("|"):
                cells = [c.strip() for c in s.strip("|").split("|") if c.strip()]
                if len(cells) >= 2 and "序号" not in cells:
                    if re.fullmatch(r"\d+", cells[0]):
                        cells = cells[1:]
                    out.append((f"{cells[0]}：{' '.join(cells[1:])}", False))
                continue
            if m := _HEADING.match(s):
                out.append((m.group(2), True))
            elif _CHAPTER.match(s) and len(s) <= 40:
                out.append((s, True))
            else:
                out.append((s, False))
    return out


def section_of(title: str, cfg: TenderConfig) -> str | None:
    t = re.sub(r"\s+", "", title)
    for sec in cfg.sections:
        if any(k in t for k in sec.keywords):
            return sec.id
    return None


def lines_of(pages: list[tuple[int, str]], cfg: TenderConfig) -> tuple[list[Line], list[dict[str, Any]]]:
    """按页顺序切行并标上所在章节。返回 (行, 识别出的章节结构)。"""
    lines: list[Line] = []
    sections: list[dict[str, Any]] = []
    names = {s.id: s.name for s in cfg.sections}
    current: str | None = None
    for page, md in pages:
        for text, is_heading in _raw_lines(md or ""):
            if is_heading:
                sid = section_of(text, cfg)
                if sid is not None:
                    current = sid
                    sections.append({"id": sid, "name": names[sid], "title": text[:60], "page": page})
                elif _CHAPTER.match(text):
                    current = None  # 新的一章但认不出类别
                    sections.append({"id": None, "name": "其他", "title": text[:60], "page": page})
            lines.append(Line(page=page, text=text, section=current, heading=is_heading))
    return lines, sections


def _has(words: list[str], text: str) -> bool:
    return any(w in text for w in words)


def classify(text: str, section: str | None, cfg: TenderConfig) -> str:
    for cat, words in cfg.categories.items():
        if _has(words, text):
            return cat
    sec = next((s for s in cfg.sections if s.id == section), None)
    return sec.category if sec else "商务"


def level_of(text: str, cfg: TenderConfig) -> str:
    m = cfg.markers
    if _has(m.star, text):
        return "star"
    if _has(m.key, text):
        return "key"
    if _has(m.must, _NOISE.sub("", text)):
        return "must"
    return "normal"


def is_requirement(text: str, cfg: TenderConfig) -> bool:
    m = cfg.markers
    t = _NOISE.sub("", text)
    return _has(m.star + m.key, text) or _has(m.must + m.disqualify + m.requirement, t)


def clause_id(section: str | None, text: str) -> str:
    norm = re.sub(r"\s+", "", text)
    return hashlib.sha1(f"{section or ''}|{norm}".encode()).hexdigest()[:10]


def _split_long(text: str, limit: int = 160) -> list[str]:
    if len(text) <= limit:
        return [text]
    lead = "★ " if text.lstrip().startswith("★") else ("▲ " if text.lstrip().startswith("▲") else "")
    parts = [p.strip() for p in re.split(r"(?<=[。；;])", text) if p.strip()]
    return [parts[0]] + [lead + p for p in parts[1:]] if parts else [text]


def clauses(lines: list[Line], cfg: TenderConfig) -> list[dict[str, Any]]:
    """需求矩阵的条款（不含人工处理结果）。同一条款在多页重复出现只保留第一次。"""
    names = {s.id: s.name for s in cfg.sections}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    n = 0
    for ln in lines:
        if ln.heading:
            continue
        for text in _split_long(ln.text):
            if not is_requirement(text, cfg):
                continue
            if ln.section == "evaluation" and _POINTS.search(text) and level_of(text, cfg) == "normal":
                continue  # 得分项放在评分点里，不进需求矩阵
            cid = clause_id(ln.section, text)
            if cid in seen:
                continue
            seen.add(cid)
            n += 1
            m = _NUMBER.match(text)
            level = level_of(text, cfg)
            cat = classify(text, ln.section, cfg)
            out.append(
                {
                    "id": cid,
                    "no": m.group(1).rstrip(".、)）") if m else f"C{n}",
                    "text": text[:1000],
                    "page": ln.page,
                    "section": ln.section,
                    "section_name": names.get(ln.section or "", "其他"),
                    "category": cat,
                    "level": level,
                    "disqualify": level == "star" or _has(cfg.markers.disqualify, text),
                    "owner_role": cfg.owners.get(cat),
                }
            )
    return out


# ---------------- 评分点 ----------------


def scoring(lines: list[Line], cfg: TenderConfig) -> list[dict[str, Any]]:
    """评标办法章节里的得分项：{item, points, page, evidence: [资料 id]}。"""
    out = []
    for ln in lines:
        if ln.section != "evaluation" or ln.heading:
            continue
        for sent in (s.strip() for s in re.split(r"[。；;]", ln.text)):
            if not _POINTS.search(sent):
                continue
            parts = [p.strip() for p in re.split(r"[，,]", sent) if p.strip()]
            pointed = [p for p in parts if _POINTS.search(p)]
            if len(pointed) >= 2 and len(pointed) == len(parts) and not _CAP_WORD.search(sent):
                items = parts
            else:
                items = [sent]
            for it in items:
                nums = [float(x) for x in _POINTS.findall(it)]
                cap = re.search(r"(?:最多|最高|满分|封顶|上限|不超过)\s*(\d+(?:\.\d+)?)\s*分", it)
                pts = float(cap.group(1)) if cap else nums[-1]
                label = re.sub(r"^.*?[：:]\s*", "", it) if "评标办法" in it or "评分办法" in it else it
                ev = [d.id for d in cfg.documents if _has(d.keywords, it)]
                out.append({"item": label[:200], "points": pts, "page": ln.page, "evidence": ev})
    return out


# ---------------- 资料清单 ----------------


def _cn(s: str) -> float | None:
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    return float(_CN_NUM[s]) if s in _CN_NUM else None


def age_limit_months(text: str) -> float | None:
    """“近一年内”“6 个月内”→ 月数。"""
    if m := re.search(r"近\s*([\d一二两三四五六七八九十半])\s*年", text):
        v = _cn(m.group(1))
        return v * 12 if v is not None else None
    if m := re.search(r"([\d一二两三四五六七八九十]+)\s*个?月内", text):
        return _cn(m.group(1))
    return None


def age_near(text: str, keywords: list[str]) -> float | None:
    """关键词附近（同一个分句里）的时效要求：“近一年内的第三方检测报告”。
    隔着顿号、逗号的不算（“近三年业绩合同、第三方检测报告”里的“近三年”说的是业绩）。"""
    for kw in keywords:
        for m in re.finditer(re.escape(kw), text):
            before = re.split(r"[、，,；;。]", text[max(0, m.start() - 20) : m.start()])[-1]
            after = re.split(r"[、，,；;。]", text[m.end() : m.end() + 20])[0]
            for part in (before, after):
                if (age := age_limit_months(part)) is not None:
                    return age
    return None


def requested_documents(lines: list[Line], cfg: TenderConfig) -> list[dict[str, Any]]:
    """招标文件要求提交的资料（不管是不是“要求”句式，提到就列）。"""
    out = []
    for d in cfg.documents:
        hits = [ln for ln in lines if not ln.heading and _has(d.keywords, ln.text)]
        if not hits:
            continue
        text = " ".join(h.text for h in hits)
        need: dict[str, Any] = {
            "id": d.id,
            "name": d.name,
            "credential_kind": d.credential_kind,
            "sources": [{"page": h.page, "quote": h.text[:300]} for h in hits[:3]],
            "star": any("★" in h.text for h in hits),
        }
        ages = [a for h in hits if (a := age_near(h.text, d.keywords)) is not None]
        if ages:
            need["max_age_months"] = min(ages)
        if d.credential_kind == "performance" and (m := re.search(r"不少于\s*(\d+)\s*[项个份]", text)):
            need["min_count"] = int(m.group(1))
        out.append(need)
    return out


# ---------------- 偏离 ----------------

_MIN = r"不得低于|不应低于|不低于|不小于|不少于|不得少于|大于等于|至少|≥|>="
_MAX = r"不得高于|不应高于|不高于|不大于|不超过|不得超过|不得大于|小于等于|≤|<="
_UNIT = r"℃|°C|个月|月|年|日历天|工作日|天|日|周"
_REQ = re.compile(
    rf"(?P<cmp>{_MIN}|{_MAX})?\s*[：:为]?\s*(?P<num>\d+(?:\.\d+)?|[一二两三四五六七八九十半])"
    rf"\s*(?P<unit>{_UNIT})?(?!\s*[%％\d])\s*(?P<post>及以上|以上|及以下|以下|以内)?"
)


def to_unit(value: float, unit: str | None, target: str) -> float | None:
    """原文单位 → 能力单位。认不出的组合返回 None（标“待确认”，不猜）。"""
    if unit is None or unit == target or (unit == "°C" and target == "℃"):
        return value
    months = {"个月": 1, "月": 1, "年": 12}
    days = {"日历天": 1, "天": 1, "日": 1, "工作日": 1.4, "周": 7, "个月": 30, "月": 30, "年": 365}
    if target == "月" and unit in months:
        return value * months[unit]
    if target == "天" and unit in days:
        return round(value * days[unit], 1)
    return None


def parse_requirement(text: str, cap: Capability) -> dict[str, Any] | None:
    """在条款里找能力指标：{rule: min/max, value（换算到能力单位）, quote}。找不到返回 None。"""
    if cap.source == "credential":
        if not _has(cap.match, text):
            return None
        age = age_near(text, cap.match)
        return {"rule": "max", "value": age, "quote": text[:200]} if age is not None else None
    for kw in cap.match:
        for km in re.finditer(re.escape(kw), text):
            tail = text[km.end() : km.end() + 40]
            m = re.match(r"[^\d一二两三四五六七八九十半%％]{0,15}?" + _REQ.pattern, tail)
            if not m or not (m.group("cmp") or m.group("unit") or m.group("post")):
                continue
            num = _cn(m.group("num"))
            if num is None:
                continue
            value = to_unit(num, m.group("unit"), cap.unit)
            if value is None:
                continue
            cmp, post = m.group("cmp") or "", m.group("post") or ""
            if cmp:
                rule = "min" if re.fullmatch(_MIN, cmp) else "max"
            elif post:
                rule = "min" if "以上" in post else "max"
            else:
                rule = cap.default_rule
            return {"rule": rule, "value": value, "quote": text[:200]}
    return None


def compare(req: dict[str, Any], ours: float | None, cap: Capability) -> str:
    """无偏离 / 正偏离 / 负偏离 / 待确认。"""
    if ours is None or req.get("value") is None:
        return "待确认"
    r = float(req["value"])
    meets = ours >= r if req["rule"] == "min" else ours <= r
    if not meets:
        return "负偏离"
    better = ours > r if cap.better == "higher" else ours < r
    return "正偏离" if better else "无偏离"
