"""进程内共享的服务：API 进程和 `fos channels run` 进程用同一套装配。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fos.agents.runtime import AgentRuntime
from fos.config import Settings
from fos.db.session import make_engine, make_sessionmaker
from fos.engineering.config import EngineeringConfig, check_engineering, load_engineering
from fos.entry.spec import check_entry
from fos.extraction.schema import ExtractionSchema, load_schemas
from fos.extraction.service import Extractor
from fos.gateway.llm import LLMClient
from fos.ledger import Ledger
from fos.ledger.report import TraceConfig, load_trace_config
from fos.onboarding.config import ImportConfig, load_import_config
from fos.ontology import Ontology
from fos.ontology.loader import load_ontology
from fos.perception.blobs import LocalBlobStore
from fos.perception.jobs import DocumentJobs
from fos.perception.ocr import OCRProvider, make_ocr
from fos.policy import Policy, load_policy
from fos.policy.approvals import ApprovalService
from fos.reports.daily import DailyConfig, load_daily_config
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
    policies: dict[str, Policy]
    ledger: Ledger
    approvals: ApprovalService
    llm: LLMClient
    ocr: OCRProvider
    blobs: LocalBlobStore
    intake: TriggerIntake
    skills: dict[str, dict[str, SkillDef]]
    runtime: AgentRuntime
    schemas: dict[str, dict[str, ExtractionSchema]]
    extractor: Extractor
    jobs: DocumentJobs
    traces: dict[str, TraceConfig]
    engineering: dict[str, EngineeringConfig | None]
    dailies: dict[str, DailyConfig]
    imports: dict[str, ImportConfig]

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
        # 审批规则同样启动时校验（事件、字段、角色必须存在）
        policies = {
            tid: load_policy(
                t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir, ontology=ontologies[tid]
            )
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
        }
        ledger = Ledger(lambda tid: ontologies[tid], lambda tid: policies.get(tid))
        approvals = ApprovalService(registry=registry, ontologies=ontologies, ledger=ledger)
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
        traces = {
            tid: load_trace_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
        }
        # 试点上线（W13）：生产日报统计项、主数据导入工作表
        dailies = {
            tid: load_daily_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
        }
        imports = {
            tid: load_import_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
        }
        check_configs(ontologies, schemas, traces, dailies, imports)
        # 工程模块：衬里模板与选材参数（没有的租户不开工程模块）
        engineering = {
            tid: load_engineering(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
        }
        for tid, cfg in engineering.items():
            if cfg is not None:
                check_engineering(tid, cfg, ontologies[tid])
        extractor = Extractor(schemas, llm, settings)
        sessionmaker = make_sessionmaker(engine)
        return cls(
            settings=settings,
            engine=engine,
            sessionmaker=sessionmaker,
            registry=registry,
            ontologies=ontologies,
            policies=policies,
            ledger=ledger,
            approvals=approvals,
            llm=llm,
            ocr=ocr,
            blobs=blobs,
            intake=TriggerIntake(
                registry=registry,
                ledger=ledger,
                ocr=ocr,
                blobs=blobs,
                settings=settings,
                extractor=extractor,
                ontologies=ontologies,
                traces=traces,
                approvals=approvals,
                dailies=dailies,
            ),
            skills=skills,
            runtime=AgentRuntime(
                registry=registry, ontologies=ontologies, skills=skills, llm=llm, settings=settings
            ),
            schemas=schemas,
            extractor=extractor,
            jobs=DocumentJobs(sessionmaker, ocr, blobs, extractor, settings),
            traces=traces,
            engineering=engineering,
            dailies=dailies,
            imports=imports,
        )

    async def aclose(self) -> None:
        await self.jobs.aclose()
        await self.ocr.aclose()
        await self.llm.aclose()
        await self.engine.dispose()


def check_configs(
    ontologies: dict[str, Ontology],
    schemas: dict[str, dict[str, ExtractionSchema]],
    traces: dict[str, TraceConfig],
    dailies: dict[str, DailyConfig] | None = None,
    imports: dict[str, ImportConfig] | None = None,
) -> None:
    """录单映射、追溯、日报、导入配置对照本体检查；有问题拒绝启动（和本体校验一样，越早发现越好）。"""
    errs: list[str] = []
    for tid, onto in ontologies.items():
        for s in schemas.get(tid, {}).values():
            if s.entry is not None:
                errs += [f"[{tid}] {e}" for e in check_entry(s.id, {f.key for f in s.fields}, s.entry, onto)]
        errs += [f"[{tid}] {e}" for e in traces[tid].check(onto)]
        if dailies and tid in dailies:
            errs += [f"[{tid}] {e}" for e in dailies[tid].check(onto)]
        if imports and tid in imports:
            errs += [f"[{tid}] {e}" for e in imports[tid].check(onto)]
    if errs:
        raise ValueError("配置与本体不一致：\n" + "\n".join(errs))
