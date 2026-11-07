"""后台解析任务（进程内）。网页上传后立即返回，解析在这里跑，进度写回 documents.progress。

- 同时处理的文档数有上限（每份文档内部再按 FOS_OCR_CONCURRENCY 并发识别页面）；
- 进程重启后，状态停在 queued / parsing / extracting 的文档会重新排队（原件已经存好）；
- 以后换成独立 Worker 进程或队列时，只换这个类，接口不变。
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from fos.config import Settings
from fos.db.models import Document
from fos.extraction.service import Extractor
from fos.perception.blobs import LocalBlobStore
from fos.perception.ocr import OCRProvider
from fos.perception.parser import ParseError, count_pages
from fos.perception.service import load_pages, parse_bytes, store_parsed

log = logging.getLogger(__name__)

ACTIVE = ("queued", "parsing", "extracting")


class DocumentJobs:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        ocr: OCRProvider,
        blobs: LocalBlobStore,
        extractor: Extractor,
        settings: Settings,
        max_docs: int = 2,
    ):
        self.sm, self.ocr, self.blobs, self.extractor, self.settings = (
            sessionmaker,
            ocr,
            blobs,
            extractor,
            settings,
        )
        self._sem = asyncio.Semaphore(max_docs)
        self._tasks: dict[str, asyncio.Task] = {}

    def submit(self, doc_id: str) -> None:
        if doc_id in self._tasks:
            return
        t = asyncio.create_task(self._run(doc_id), name=f"doc:{doc_id}")
        self._tasks[doc_id] = t
        t.add_done_callback(lambda _t: self._tasks.pop(doc_id, None))

    async def wait(self, doc_id: str) -> None:
        t = self._tasks.get(doc_id)
        if t is not None:
            await asyncio.shield(t)

    async def recover(self) -> int:
        async with self.sm() as s:
            ids = list((await s.scalars(select(Document.id).where(Document.status.in_(ACTIVE)))).all())
        for i in ids:
            self.submit(i)
        return len(ids)

    async def aclose(self) -> None:
        for t in list(self._tasks.values()):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _set(self, doc_id: str, **kw) -> None:
        async with self.sm() as s, s.begin():
            d = await s.get(Document, doc_id)
            if d is not None:
                for k, v in kw.items():
                    setattr(d, k, v)

    async def _run(self, doc_id: str) -> None:
        async with self._sem:
            try:
                await self._process(doc_id)
            except asyncio.CancelledError:
                raise  # 进程退出：保持 parsing 状态，重启后续跑
            except Exception as e:  # 任何意外都落到 failed，界面上能看到原因并重试
                log.exception("文档 %s 处理失败", doc_id)
                await self._set(doc_id, status="failed", error=f"处理失败：{e}"[:500])

    async def _process(self, doc_id: str) -> None:
        async with self.sm() as s:
            d = await s.get(Document, doc_id)
            if d is None or d.status not in ACTIVE:
                return
            tenant_id, digest = d.tenant_id, d.sha256
        data = self.blobs.get(tenant_id, digest)
        await self._set(doc_id, status="parsing", progress=0, page_count=count_pages(data))

        last = 0.0

        async def on_progress(done: int, total: int) -> None:
            nonlocal last
            now = time.monotonic()
            if done >= total or now - last >= 0.5:  # 限制写库频率
                last = now
                await self._set(doc_id, progress=done, page_count=total)

        try:
            parsed = await parse_bytes(data, self.ocr, self.settings, on_progress)
        except ParseError as e:
            await self._set(doc_id, status="failed", error=str(e)[:500])
            return

        async with self.sm() as s, s.begin():
            d = await s.get(Document, doc_id)
            if d is None:
                return
            pages = await store_parsed(s, d, parsed, self.ocr)
            if pages:
                d.status = "extracting"
        if not pages:
            return
        async with self.sm() as s, s.begin():
            d = await s.get(Document, doc_id)
            if d is None:
                return
            await self.extractor.run(s, d, await load_pages(s, doc_id))
            d.status = "parsed"
