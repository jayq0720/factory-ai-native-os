"""Agent 运行时：接手 Trigger 入口交过来的消息（route=router），走 Router → Worker，产出回复和待确认卡片。

事务边界：一次处理在一个事务里完成（运行记录、卡片记录、Trigger 状态），提交之后再发回复和卡片。
Thread 直通：同一 Thread 里有等待用户回答的运行时，下一条消息直接送回那个运行，不经过 Router。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fos.agents.router import decide
from fos.agents.worker import run_worker, system_prompt
from fos.channels.base import ChannelAdapter
from fos.channels.cards import Card, CardButton, CardField
from fos.channels.types import ConversationRef
from fos.config import Settings
from fos.db.models import CardRecord, Run, Trigger, User, utcnow
from fos.gateway.llm import LLMClient
from fos.governance.principal import principal_for_user
from fos.ledger.service import AppendRequest, Evidence
from fos.ontology import Ontology
from fos.skills.schema import SkillDef
from fos.tenancy import TenantRegistry
from fos.tools.registry import Proposal, ToolContext
from fos.triggers.cards import LedgerAppendAction, propose_card, send_card_record
from fos.triggers.intake import Outcome, deliver

log = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    run: Run | None = None
    decision: str = ""  # dispatch / resume / reply / ignore / skip
    replies: list[str] = field(default_factory=list)
    cards: list[CardRecord] = field(default_factory=list)
    to: ConversationRef | None = None


class AgentRuntime:
    def __init__(
        self,
        *,
        registry: TenantRegistry,
        ontologies: dict[str, Ontology],
        skills: dict[str, dict[str, SkillDef]],
        llm: LLMClient,
        settings: Settings,
    ):
        self.registry = registry
        self.ontologies = ontologies
        self.skills = skills
        self.llm = llm
        self.settings = settings

    async def process(self, sm: async_sessionmaker, adapter: ChannelAdapter, trigger_id: str) -> RunOutcome:
        async with sm() as session, session.begin():
            out = await self._process(session, trigger_id)
        if out.to is not None:  # 提交之后再发
            box = Outcome(None, outbox=[(out.to, r) for r in out.replies])
            await deliver(adapter, box)
            for rec in out.cards:
                err = await send_card_record(adapter, rec)
                if err:
                    async with sm() as session, session.begin():
                        r = await session.get(CardRecord, rec.id)
                        if r is not None:
                            r.status, r.result = "failed", {"error": err}
        return out

    async def _process(self, session: AsyncSession, trigger_id: str) -> RunOutcome:
        trig = await session.get(Trigger, trigger_id)
        if trig is None or trig.route != "router" or trig.status != "pending" or not trig.user_id:
            return RunOutcome(decision="skip")
        tenant = self.registry.get(trig.tenant_id)
        user = await session.get(User, trig.user_id)
        if tenant is None or user is None:
            return RunOutcome(decision="skip")
        principal = principal_for_user(user, tenant)
        to = ConversationRef(
            channel=trig.source,
            conversation_id=trig.payload.get("conversation_id", ""),
            conversation_type=trig.payload.get("conversation_type", "group"),
            user_id=trig.sender_ref if trig.payload.get("conversation_type") == "direct" else None,
        )
        out = RunOutcome(to=to)
        skills = self.skills.get(trig.tenant_id, {})

        waiting = (
            await session.scalars(
                select(Run)
                .where(
                    Run.tenant_id == trig.tenant_id,
                    Run.thread_id == trig.thread_id,
                    Run.status == "waiting",
                    Run.user_id == trig.user_id,
                )
                .order_by(Run.updated_at.desc())
                .limit(1)
            )
        ).one_or_none()
        if waiting is not None and waiting.skill_id in skills:
            out.decision = "resume"
            run = waiting
            run.messages = [
                *run.messages,
                {"role": "user", "content": f"<user_reply>\n{trig.text}\n</user_reply>"},
            ]
        else:
            context = await self._recent(session, trig)
            d, res = await decide(
                self.llm,
                factory=tenant.name,
                tenant_id=tenant.id,
                text=trig.text,
                context=context,
                skills=skills,
            )
            if d.action != "dispatch":
                out.decision = d.action
                trig.route, trig.status = "router", "done"
                trig.result = {
                    "decision": d.action,
                    **({"tokens": res.prompt_tokens + res.completion_tokens} if res else {}),
                }
                if d.action == "reply" and d.reply:
                    out.replies.append(d.reply)
                return out
            out.decision = "dispatch"
            skill = skills[d.skill or ""]
            ctx0 = ToolContext(
                session=session, user=principal, skill=skill, ontology=self.ontologies[tenant.id]
            )
            run = Run(
                tenant_id=tenant.id,
                thread_id=trig.thread_id,
                trigger_id=trig.id,
                user_id=principal.subject_id,
                skill_id=skill.id,
                skill_version=skill.version,
                status="running",
                task=d.task,
                messages=[
                    {"role": "system", "content": system_prompt(tenant.name, ctx0)},
                    {"role": "user", "content": _task_block(d.task, context)},
                ],
                steps=[],
                result={},
                data_mode="real",
            )
            session.add(run)
            await session.flush()

        skill = skills[run.skill_id]
        ctx = ToolContext(session=session, user=principal, skill=skill, ontology=self.ontologies[tenant.id])
        wr = await run_worker(
            self.llm,
            ctx,
            factory=tenant.name,
            messages=run.messages,
            max_tokens=self.settings.agent_max_tokens,
            result_chars=self.settings.agent_tool_result_chars,
        )
        run.messages = wr.messages
        run.steps = [*run.steps, *wr.steps]
        run.status = wr.status
        run.data_mode = "mock" if (wr.data_mode == "mock" or run.data_mode == "mock") else "real"
        run.prompt_tokens += wr.prompt_tokens
        run.completion_tokens += wr.completion_tokens
        run.updated_at = utcnow()
        if wr.reply:
            out.replies.append(wr.reply)

        for p in ctx.proposals if wr.status == "done" else []:
            rec = await propose_card(
                session,
                tenant_id=tenant.id,
                to=to,
                thread_id=run.thread_id,
                card=_proposal_card(self.ontologies[tenant.id], p, run.data_mode, skill),
                created_by=f"agent:{skill.id}",
                action=LedgerAppendAction(
                    request=AppendRequest(
                        type=p.type,
                        object_id=p.object_id,
                        payload=p.payload,
                        evidence=[Evidence.model_validate(e) for e in p.evidence],
                        data_mode=run.data_mode,
                        correlation_id=run.thread_id,
                    )
                ),
            )
            out.cards.append(rec)
        run.result = {"reply": wr.reply, "cards": [c.id for c in out.cards]}
        trig.route, trig.status = "run", "done"
        trig.result = {"run_id": run.id, "skill": run.skill_id, "decision": out.decision}
        out.run = run
        await session.flush()
        return out

    async def _recent(self, session: AsyncSession, trig: Trigger) -> list[str]:
        rows = (
            await session.scalars(
                select(Trigger)
                .where(
                    Trigger.tenant_id == trig.tenant_id,
                    Trigger.thread_id == trig.thread_id,
                    Trigger.id != trig.id,
                    Trigger.kind == "text",
                )
                .order_by(Trigger.created_at.desc())
                .limit(5)
            )
        ).all()
        return [t.text[:200] for t in reversed(rows) if t.text]


def _task_block(task: str, context: list[str]) -> str:
    recent = "\n".join(f"- {c}" for c in context)
    return f"<task>\n{task}\n</task>\n<recent_messages>\n{recent}\n</recent_messages>"


def _proposal_card(onto: Ontology, p: Proposal, data_mode: str, skill: SkillDef) -> Card:
    ev = onto.events[p.type]
    props = onto.objects[ev.object].properties
    fields = [
        CardField(label=(props[k].name if k in props else k)[:20], value=_fmt(v)[:200])
        for k, v in list(p.payload.items())[:12]
    ]
    return Card(
        title=p.title[:40],
        text=f"{skill.role}提议记录“{ev.name}”，确认后入账。",
        fields=fields,
        buttons=[
            CardButton(id="confirm", label="确认", style="primary"),
            CardButton(id="cancel", label="取消"),
        ],
        data_mode=data_mode,  # type: ignore[arg-type]
        footer=f"技能：{skill.name} v{skill.version}",
    )


def _fmt(v: Any) -> str:
    if isinstance(v, list):
        return "、".join(map(str, v))
    return str(v)
