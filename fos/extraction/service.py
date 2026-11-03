"""对一份已解析的文档运行字段抽取，结果写进 document_fields。

重新抽取时，人工修改过或确认过的字段保持原值，只刷新候选值，避免把人的判断冲掉。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.config import LLMMode, Settings
from fos.db.models import Document, DocumentField, DocumentPage
from fos.extraction.llm import extract_llm
from fos.extraction.merge import combine, resolve
from fos.extraction.rules import extract_rules
from fos.extraction.schema import ExtractionSchema, schema_for_kind
from fos.gateway.llm import LLMClient

log = logging.getLogger(__name__)

HUMAN_STATUSES = ("corrected", "confirmed")


async def load_fields(session: AsyncSession, doc_id: str) -> list[DocumentField]:
    return list(
        (await session.scalars(select(DocumentField).where(DocumentField.document_id == doc_id))).all()
    )


class Extractor:
    def __init__(self, schemas: dict[str, dict[str, ExtractionSchema]], llm: LLMClient, settings: Settings):
        self.schemas = schemas  # 租户 id → Schema id → Schema
        self.llm = llm
        self.settings = settings

    @property
    def uses_llm(self) -> bool:
        return self.settings.extract_with_llm and self.llm.mode is LLMMode.litellm

    def schema_for(self, tenant_id: str, kind: str, schema_id: str | None = None) -> ExtractionSchema | None:
        ts = self.schemas.get(tenant_id, {})
        if schema_id:
            return ts.get(schema_id)
        return schema_for_kind(ts, kind)

    async def run(
        self, session: AsyncSession, doc: Document, pages: list[DocumentPage]
    ) -> list[DocumentField] | None:
        schema = self.schema_for(doc.tenant_id, doc.kind, doc.schema_id)
        if schema is None or doc.status == "failed":
            return None
        doc.schema_id = schema.id
        texts = [(p.page, p.markdown) for p in pages]
        rule = extract_rules(schema, texts)
        llm = {}
        if self.uses_llm:
            llm = await extract_llm(
                schema, texts, self.llm, tenant_id=doc.tenant_id, max_chars=self.settings.extract_chunk_chars
            )
        modes = {p.page: p.data_mode for p in pages}
        existing = {f.key: f for f in await load_fields(session, doc.id)}
        now = datetime.now(UTC)
        out: list[DocumentField] = []
        for fd in schema.fields:
            r = resolve(fd, combine(rule.get(fd.key, []), llm.get(fd.key, [])), modes, doc.data_mode)
            row = existing.pop(fd.key, None)
            if row is None:
                row = DocumentField(document_id=doc.id, key=fd.key, tenant_id=doc.tenant_id, history=[])
                session.add(row)
            row.name, row.group_name, row.type, row.required = fd.name, fd.group, fd.type, fd.required
            row.candidates = r.candidates
            if row.status not in HUMAN_STATUSES:
                row.value, row.unit, row.status, row.method = r.value, r.unit, r.status, r.method
                row.confidence, row.conflict, row.sources = r.confidence, r.conflict, r.sources
                row.data_mode = r.data_mode
            else:
                row.conflict = r.conflict
            row.updated_at = now
            out.append(row)
        for stale in existing.values():  # Schema 里删掉的字段
            await session.delete(stale)
        await session.flush()
        return out
