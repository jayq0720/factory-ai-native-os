"""模型抽取：把逐页原文分块交给 fos-chat（DeepSeek-V3 等），要求每个字段给出页码和原文片段。

防编造：模型给的原文片段必须能在那一页原文里找到、值必须出现在片段里，否则丢弃。
模型只负责"读"，不做任何换算或计算（规则 3）。
"""

from __future__ import annotations

import json
import logging
import re

from fos.extraction.rules import Candidate, Source, group, segments
from fos.extraction.schema import ExtractionSchema, FieldDef
from fos.gateway.llm import ChatMessage, LLMClient, LLMError

log = logging.getLogger(__name__)

SYSTEM = """你是工业技术文件、招标文件的信息抽取助手。
只根据给出的原文抽取字段，不推测、不换算、不补充原文没有的内容。
输出 JSON：{"fields": [{"key": 字段 key, "value": 值, "unit": 单位或 null, "page": 页码, "quote": 原文片段}]}
要求：
1. quote 必须是原文里连续的一段文字，逐字照抄，不超过 80 个字，并且包含 value。
2. 原文里找不到的字段不要输出。同一字段在多页出现且值不同，每处各输出一条。
3. 数值保留原文写法，单位单独放在 unit；枚举字段的 value 只能从给出的取值里选。"""


def _norm(s: str) -> str:
    return re.sub(r"[\s|]+", "", s).lower()


def _field_line(f: FieldDef) -> str:
    extra = []
    if f.unit:
        extra.append(f"单位 {f.unit}")
    if f.enum:
        extra.append("取值：" + "/".join(f.enum))
    if f.multi or f.type == "list":
        extra.append("可多个")
    if f.description:
        extra.append(f.description)
    if f.labels:
        extra.append("常见叫法：" + "、".join(f.labels[:4]))
    return f"- {f.key}｜{f.name}｜{f.type}" + ("｜" + "；".join(extra) if extra else "")


def page_texts(pages: list[tuple[int, str]]) -> dict[int, str]:
    return {p: "\n".join(s.text for s in segments(p, md)) for p, md in pages}


def chunks(texts: dict[int, str], max_chars: int) -> list[list[int]]:
    out: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for p, t in texts.items():
        if cur and size + len(t) > max_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += len(t)
    if cur:
        out.append(cur)
    return out


def validate(f: FieldDef, item: dict, texts: dict[int, str], pages: list[int]) -> tuple | None:
    try:
        page = int(item.get("page"))
    except (TypeError, ValueError):
        return None
    value = str(item.get("value") or "").strip()
    quote = str(item.get("quote") or "").strip()
    if page not in pages or not value or not quote:
        return None
    nq = _norm(quote)
    if not nq or nq not in _norm(texts[page]):
        return None  # 原文里找不到这段话：丢弃
    if f.type in ("number", "temperature", "money"):
        num = re.search(r"-?\d+(?:\.\d+)?", value)
        if not num or num.group(0) not in quote:
            return None
        value = num.group(0)
    elif f.type == "enum":
        if value not in f.enum or value not in quote:
            return None
    elif _norm(value) not in nq:
        return None
    unit = item.get("unit") or f.unit
    unit = str(unit)[:16] if unit else None
    highlight = [p.strip() for p in quote.split("|") if p.strip()]
    return value, unit, Source(page, quote, highlight), 0.8


async def extract_llm(
    schema: ExtractionSchema,
    pages: list[tuple[int, str]],
    llm: LLMClient,
    *,
    tenant_id: str,
    max_chars: int = 6000,
) -> dict[str, list[Candidate]]:
    texts = page_texts(pages)
    spec = "\n".join(_field_line(f) for f in schema.fields)
    raw: dict[str, list[tuple]] = {f.key: [] for f in schema.fields}
    fields = {f.key: f for f in schema.fields}
    for chunk in chunks(texts, max_chars):
        body = "\n\n".join(f"=== 第 {p} 页 ===\n{texts[p][:max_chars]}" for p in chunk)
        msgs = [
            ChatMessage(role="system", content=SYSTEM),
            ChatMessage(role="user", content=f"字段清单：\n{spec}\n\n原文：\n{body}"),
        ]
        try:
            r = await llm.chat(msgs, model="fos-chat", tenant_id=tenant_id, json_mode=True)
            items = json.loads(r.content).get("fields") or []
        except (LLMError, ValueError, AttributeError) as e:
            log.warning("模型抽取失败（第 %s–%s 页），只用规则结果：%s", chunk[0], chunk[-1], e)
            continue
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or (f := fields.get(str(item.get("key")))) is None:
                continue
            v = validate(f, item, texts, chunk)
            if v is not None:
                raw[f.key].append(v)
    return {k: group(v, method="llm") for k, v in raw.items()}
