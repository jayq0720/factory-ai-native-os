"""进程内共享的服务：API 进程和 `fos channels run` 进程用同一套装配。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fos.agents.runtime import AgentRuntime
from fos.config import Settings
from fos.db.session import make_engine, make_sessionmaker
from fos.extraction.schema import ExtractionSchema, load_schemas
from fos.extraction.service import Extractor
from fos.gateway.llm import LLMClient
from fos.ledger import Ledger
from fos.ontology import Ontology
from fos.ontology.loader import load_ontology
from fos.perception.blobs import LocalBlobStore
from fos.perception.jobs import DocumentJobs
from fos.perception.ocr import OCRProvider, make_ocr
from fos.skills.loader import load_skills
from fos.skills.schema import SkillDef
from fos.tenancy import TenantRegistry
from fos.tools import builtin  # noqa: F401  注册内置工具
from fos.tools.registry import REGISTRY
from fos.triggers.intake import TriggerIntake


@dataclass
class Services:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker
    registry: TenantRegistry
    ontologies: dict[str, Ontology]
    ledger: Ledger
    llm: LLMClient
    ocr: OCRProvider
    blobs: LocalBlobStore
    intake: TriggerIntake
    skills: dict[str, dict[str, SkillDef]]
    runtime: AgentRuntime
    schemas: dict[str, dict[str, ExtractionSchema]]
    extractor: Extractor
    jobs: DocumentJobs

    @classmethod
    def build(cls, settings: Settings) -> Services:
        engine = make_engine(settings.database_url)
        registry = TenantRegistry.load(settings.tenants_dir)
        # 启动时加载并校验每个租户的本体；任何一个不合法就拒绝启动
        ontologies = {
            tid: load_ontology(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
        }
        ledger = Ledger(lambda tid: ontologies[tid])
        skills = {
            tid: load_skills(t, packs_dir=settings.packs_dir, ontology=ontologies[tid], tools=set(REGISTRY))
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
        }
        llm = LLMClient.from_settings(settings)
        ocr = make_ocr(settings)
        blobs = LocalBlobStore(settings.blob_dir)
        schemas = {
            tid: load_schemas(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
        }
        extractor = Extractor(schemas, llm, settings)
        sessionmaker = make_sessionmaker(engine)
        return cls(
            settings=settings,
            engine=engine,
            sessionmaker=sessionmaker,
            registry=registry,
            ontologies=ontologies,
            ledger=ledger,
            llm=llm,
            ocr=ocr,
            blobs=blobs,
            intake=TriggerIntake(registry=registry, ledger=ledger, ocr=ocr, blobs=blobs, settings=settings),
            skills=skills,
            runtime=AgentRuntime(
                registry=registry, ontologies=ontologies, skills=skills, llm=llm, settings=settings
            ),
            schemas=schemas,
            extractor=extractor,
            jobs=DocumentJobs(sessionmaker, ocr, blobs, extractor, settings),
        )

    async def aclose(self) -> None:
        await self.jobs.aclose()
        await self.ocr.aclose()
        await self.llm.aclose()
        await self.engine.dispose()
