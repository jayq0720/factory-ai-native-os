"""把规则和模型的候选值合并成每个字段的最终结果：选值、标冲突、标缺失、定 data_mode。"""

from __future__ import annotations

from dataclasses import dataclass, field

from fos.extraction.rules import MAX_SOURCES, Candidate
from fos.extraction.schema import FieldDef

CONFLICT_MIN_CONF = 0.7


@dataclass
class FieldResult:
    key: str
    value: str | None
    unit: str | None
    status: str  # extracted / missing
    method: str
    confidence: float
    conflict: bool
    sources: list[dict]
    candidates: list[dict] = field(default_factory=list)
    data_mode: str = "real"


def combine(rule: list[Candidate], llm: list[Candidate]) -> list[Candidate]:
    """同值的规则候选与模型候选合并，两边都找到的可信度更高。"""
    out = {
        (c.value, c.unit): Candidate(c.value, c.unit, list(c.sources), c.confidence, c.method) for c in rule
    }
    for c in llm:
        k = (c.value, c.unit)
        if k in out:
            o = out[k]
            o.method = "rule+llm"
            o.confidence = min(0.95, max(o.confidence, c.confidence) + 0.1)
            for s in c.sources:
                if not any(x.page == s.page and x.quote == s.quote for x in o.sources):
                    o.sources.append(s)
        else:
            out[k] = Candidate(c.value, c.unit, list(c.sources), c.confidence, c.method)
    return list(out.values())


def _first_page(c: Candidate) -> int:
    return min(s.page for s in c.sources)


def resolve(f: FieldDef, cands: list[Candidate], page_modes: dict[int, str], doc_mode: str) -> FieldResult:
    if not cands:
        return FieldResult(f.key, None, f.unit, "missing", "none", 0.0, False, [], [], doc_mode)
    ordered = sorted(cands, key=lambda c: (_first_page(c), -c.confidence))
    dumped = [c.dump() for c in sorted(cands, key=lambda c: (-c.confidence, _first_page(c)))]

    if f.multi or f.type == "list":
        # 低可信度的全文扫描命中，只在没有更好的命中时才用
        best = max(c.confidence for c in cands)
        keep = [c for c in ordered if c.confidence >= min(best, 0.6)]
        units = {c.unit for c in keep}
        unit = units.pop() if len(units) == 1 else None
        vals = list(
            dict.fromkeys(c.value if unit is not None or not c.unit else f"{c.value} {c.unit}" for c in keep)
        )
        if f.type == "list" and (len(vals) > 12 or sum(map(len, vals)) > 300):
            value = f"共 {len(vals)} 条"
        else:
            value = ("、" if f.type == "enum" else "；").join(vals)
        sources = [s for c in keep for s in c.sources][: MAX_SOURCES * 4]
        methods = {c.method for c in keep}
        method = methods.pop() if len(methods) == 1 else "rule+llm"
        conf = max(c.confidence for c in keep)
        conflict = False
    else:
        pick = sorted(cands, key=lambda c: (-c.confidence, _first_page(c)))[0]
        value, unit, method, conf = pick.value, pick.unit, pick.method, pick.confidence
        sources = pick.sources[:MAX_SOURCES]
        conflict = any(
            c is not pick and c.confidence >= CONFLICT_MIN_CONF and c.value != pick.value for c in cands
        )
    pages = {s.page for c in cands for s in c.sources}
    mode = "mock" if any(page_modes.get(p) == "mock" for p in pages) else "real"
    return FieldResult(
        f.key, value, unit, "extracted", method, conf, conflict, [s.dump() for s in sources], dumped, mode
    )
