"""进程内共享的服务：API 进程和 `fos channels run` 进程用同一套装配。"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fos.agents.runtime import AgentRuntime
from fos.config import Settings
from fos.db.session import make_engine, make_sessionmaker
from fos.eam import tools as eam_tools  # noqa: F401  注册设备管理工具
from fos.eam.config import EamConfig, load_eam_config
from fos.eam.guards import (
    EQUIP_RETIRE_EVENT,
    PM_DONE_EVENT,
    WO_DONE_EVENT,
    WO_START_EVENT,
    EamGuard,
)
from fos.ehs import tools as ehs_tools  # noqa: F401  注册 EHS 安全环保工具
from fos.ehs.config import EhsConfig, load_ehs_config
from fos.ehs.guards import (
    ENV_RECORD_EVENT,
    HAZARD_RECTIFY_EVENT,
    HAZARD_VERIFY_EVENT,
    INCIDENT_CLOSE_EVENT,
    PERMIT_CLOSE_EVENT,
    PERMIT_ISSUE_EVENT,
    EhsGuard,
)
from fos.engineering.config import EngineeringConfig, check_engineering, load_engineering
from fos.engineering.service import QUOTE_EVENT, EngQuoteGuard
from fos.entry.spec import check_entry
from fos.extraction.schema import ExtractionSchema, load_schemas
from fos.extraction.service import Extractor
from fos.finance import tools as finance_tools  # noqa: F401  注册财务与成本工具
from fos.finance.config import FinanceConfig, load_finance_config
from fos.finance.guards import (
    AP_PAY_EVENT,
    AP_RECORD_EVENT,
    AR_RECEIVE_EVENT,
    AR_RECORD_EVENT,
    VOUCHER_DRAFT_EVENT,
    VOUCHER_POST_EVENT,
    FinanceGuard,
)
from fos.gateway.llm import LLMClient
from fos.ledger import Ledger
from fos.ledger.report import TraceConfig, load_trace_config
from fos.monitoring.config import MonitorConfig, load_monitor_config
from fos.monitoring.edge import CommandSigner
from fos.onboarding.config import ImportConfig, load_import_config
from fos.ontology import Ontology
from fos.ontology.loader import load_ontology
from fos.orders import tools as order_tools  # noqa: F401  注册订单工具
from fos.orders.config import OrdersConfig, load_orders_config
from fos.orders.shipping import SHIP_EVENT, ShipmentGuard
from fos.perception.blobs import LocalBlobStore
from fos.perception.jobs import DocumentJobs
from fos.perception.ocr import OCRProvider, make_ocr
from fos.planning import tools as planning_tools  # noqa: F401  注册计划排窑工具
from fos.planning.config import PlanningConfig, load_planning_config
from fos.planning.guard import RUN_EVENT, PlannedRunGuard
from fos.policy import Policy, load_policy
from fos.policy.approvals import ApprovalService
from fos.quality import tools as quality_tools  # noqa: F401  注册质量体系工具
from fos.quality.config import QualityConfig, load_quality_config
from fos.quality.guards import (
    INST_CAL_EVENT,
    NCR_8D_EVENT,
    NCR_CLOSE_EVENT,
    NCR_DISPOSE_EVENT,
    CalibrationGuard,
    NcrGuard,
)
from fos.reports.daily import DailyConfig, load_daily_config
from fos.skills.loader import load_skills
from fos.skills.schema import SkillDef
from fos.supply import tools as supply_tools  # noqa: F401  注册采购仓储工具
from fos.supply.config import SupplyConfig, load_supply_config
from fos.supply.guards import COUNT_EVENT, ISSUE_EVENT, PO_EVENT, RECEIVE_EVENT, RETURN_EVENT, SupplyGuard
from fos.tenancy import TenantRegistry
from fos.tender.config import TenderConfig, load_tender_config
from fos.tender.service import SUBMIT_EVENT, TenderSubmitGuard
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
    orders: dict[str, OrdersConfig]
    planning: dict[str, PlanningConfig]
    supply: dict[str, SupplyConfig]
    tenders: dict[str, TenderConfig]
    monitoring: dict[str, MonitorConfig]
    quality: dict[str, QualityConfig]
    finance: dict[str, FinanceConfig]
    eam: dict[str, EamConfig]
    ehs: dict[str, EhsConfig]
    # fos-edge 命令签名（W21）：私钥来自环境变量 FOS_EDGE_COMMAND_KEY；没配置为 None，不能下发命令
    edge_signer: CommandSigner | None = None

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
        # 订单交付（W14）：发货前的跨对象校验注册为账本守卫
        orders = {
            tid: load_orders_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
        }
        ledger.add_guard(SHIP_EVENT, ShipmentGuard(orders))
        # 计划排窑（W16）：只给本体里有窑炉和排窑计划的租户开
        planning = {
            tid: load_planning_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None and "kiln_plan" in ontologies[tid].objects
        }
        ledger.add_guard(RUN_EVENT, PlannedRunGuard(orders))
        # 采购与仓储（W17）：领料 / 退料 / 盘点、按采购单到货及采购下单的跨对象校验注册为账本守卫
        supply = {
            tid: load_supply_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None and "stock_move" in ontologies[tid].objects
        }
        for ev in (ISSUE_EVENT, RETURN_EVENT, COUNT_EVENT, RECEIVE_EVENT, PO_EVENT):
            ledger.add_guard(ev, SupplyGuard(ev, supply, orders))
        # 工程报价发布与招标提交守卫：防止绕过业务接口直接 POST /v1/events 伪造毛利率或红线字段
        ledger.add_guard(QUOTE_EVENT, EngQuoteGuard())
        ledger.add_guard(SUBMIT_EVENT, TenderSubmitGuard())
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
        # 招标拆解（W18）：只给本体里有招标项目、行业包有 tender.yaml 的租户开
        tenders: dict[str, TenderConfig] = {}
        for tid in registry.ids():
            t = registry.get(tid)
            if t is None or "tender" not in ontologies[tid].objects:
                continue
            tcfg = load_tender_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            if tcfg is None:
                continue
            errs = tcfg.check(ontologies[tid])
            if errs:
                raise ValueError(f"[{tid}] 招标配置与本体不一致：\n" + "\n".join(errs))
            tenders[tid] = tcfg
        # 窑炉监控（W19）：只给本体里有测点的租户开
        monitoring = {
            tid: load_monitor_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None and "monitor_point" in ontologies[tid].objects
        }
        # 质量体系（W22）：只给本体里有不合格品报告（ncr）的租户开
        quality = {
            tid: load_quality_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None and "ncr" in ontologies[tid].objects
        }
        for ev in (NCR_DISPOSE_EVENT, NCR_8D_EVENT, NCR_CLOSE_EVENT):
            ledger.add_guard(ev, NcrGuard(ev, quality))
        ledger.add_guard(INST_CAL_EVENT, CalibrationGuard())
        # 财务集成与成本核算（W23）：只给本体里有应收单（receivable）的租户开
        finance = {
            tid: cfg
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
            and "receivable" in ontologies[tid].objects
            and (
                cfg := load_finance_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir)
            )
            is not None
        }
        for ev in (
            AR_RECORD_EVENT,
            AR_RECEIVE_EVENT,
            AP_RECORD_EVENT,
            AP_PAY_EVENT,
            VOUCHER_DRAFT_EVENT,
            VOUCHER_POST_EVENT,
        ):
            ledger.add_guard(ev, FinanceGuard(ev))
        # 设备管理（W24）：只给本体里有保养计划（maintenance_plan）的租户开
        eam = {
            tid: cfg
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
            and "maintenance_plan" in ontologies[tid].objects
            and (cfg := load_eam_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir))
            is not None
        }
        for ev in (PM_DONE_EVENT, WO_START_EVENT, WO_DONE_EVENT, EQUIP_RETIRE_EVENT):
            ledger.add_guard(ev, EamGuard(ev))
        # EHS 安全环保（W25）：只给本体里有安全隐患（safety_hazard）的租户开
        ehs = {
            tid: cfg
            for tid in registry.ids()
            if (t := registry.get(tid)) is not None
            and "safety_hazard" in ontologies[tid].objects
            and (cfg := load_ehs_config(t, packs_dir=settings.packs_dir, tenants_dir=settings.tenants_dir))
            is not None
        }
        for ev in (
            HAZARD_RECTIFY_EVENT,
            HAZARD_VERIFY_EVENT,
            PERMIT_ISSUE_EVENT,
            PERMIT_CLOSE_EVENT,
            INCIDENT_CLOSE_EVENT,
            ENV_RECORD_EVENT,
        ):
            ledger.add_guard(ev, EhsGuard(ev, ehs))
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
                orders=orders,
                planning=planning,
                supply=supply,
                tenders=tenders,
                monitoring=monitoring,
                quality=quality,
                finance=finance,
                eam=eam,
                ehs=ehs,
            ),
            skills=skills,
            runtime=AgentRuntime(
                registry=registry,
                ontologies=ontologies,
                skills=skills,
                llm=llm,
                settings=settings,
                tool_extras={
                    tid: {
                        "orders": orders[tid],
                        "planning": planning.get(tid),
                        "supply": supply.get(tid),
                        "quality": quality.get(tid),
                        "finance": finance.get(tid),
                        "eam": eam.get(tid),
                        "ehs": ehs.get(tid),
                        "tz": ZoneInfo(t.timezone),
                    }
                    for tid in registry.ids()
                    if (t := registry.get(tid)) is not None
                },
            ),
            schemas=schemas,
            extractor=extractor,
            jobs=DocumentJobs(sessionmaker, ocr, blobs, extractor, settings),
            traces=traces,
            engineering=engineering,
            dailies=dailies,
            imports=imports,
            orders=orders,
            planning=planning,
            supply=supply,
            tenders=tenders,
            monitoring=monitoring,
            quality=quality,
            finance=finance,
            eam=eam,
            ehs=ehs,
            edge_signer=CommandSigner.from_setting(settings.edge_command_key),
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
