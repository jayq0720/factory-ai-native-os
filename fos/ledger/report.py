"""批次追溯报告（方案 3.3 链式召回，验收：任一成品批次 10 秒内追溯到原料批次、窑次、检验结果）。

trace.py 是不分方向的引用图展开；这里按方向整理成人看得懂的报告：
- 上游（它用了什么）：沿引用往外走，生产批次 → 原料批次 → 供应商 / 物料，生产批次 → 窑次 → 窑炉；
- 下游（谁用了它）：沿反向引用走，原料批次 → 用了它的生产批次 → 这些批次的检验；
- 旁证：上游对象的其他关联（来料检验、同一窑次 / 同一批原料的其他生产批次），排查影响范围用。

告警规则、追溯入口和显示字段写在 packs/<pack>/trace.yaml（租户可在 tenants/<id>/trace.yaml 追加），
不写 Python。全部是确定性查询，不调用模型。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fos.channels.cards import Card, CardField
from fos.db.models import ObjectLink, ObjectState
from fos.governance.principal import Principal
from fos.ontology import Ontology
from fos.tenancy.config import TenantConfig

MAX_NODES = 300


class TraceRoot(BaseModel):
    object: str
    keys: list[str] = Field(default_factory=list)  # 除对象编号外，还能按哪些属性找（窑次号、供方批号）


class AlertRule(BaseModel):
    object: str
    field: str
    values: list[Any] = Field(alias="in")
    text: str


class TraceConfig(BaseModel):
    roots: list[TraceRoot] = Field(default_factory=list)
    alerts: list[AlertRule] = Field(default_factory=list)
    show: dict[str, list[str]] = Field(default_factory=dict)  # 每类对象在报告里显示哪些属性
    value_names: dict[str, str] = Field(default_factory=dict)  # 枚举值的中文

    def check(self, onto: Ontology) -> list[str]:
        errs = []
        for r in self.roots:
            if r.object not in onto.objects:
                errs.append(f"trace.roots: 没有对象类型 {r.object}")
            else:
                errs += [
                    f"trace.roots: {r.object} 没有属性 {k}"
                    for k in r.keys
                    if k not in onto.objects[r.object].properties
                ]
        for a in self.alerts:
            if a.object not in onto.objects or a.field not in onto.objects[a.object].properties:
                errs.append(f"trace.alerts: 没有 {a.object}.{a.field}")
        for t, props in self.show.items():
            if t not in onto.objects:
                errs.append(f"trace.show: 没有对象类型 {t}")
            else:
                have = onto.objects[t].properties
                errs += [f"trace.show: {t} 没有属性 {p}" for p in props if p not in have]
        return errs


def _read(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load_trace_config(tenant: TenantConfig, *, packs_dir: Path, tenants_dir: Path) -> TraceConfig:
    """行业包 + 租户：租户的入口排在前面，告警追加，显示字段和枚举中文按对象覆盖。"""
    pack = _read(packs_dir / tenant.industry_pack / "trace.yaml")
    own = _read(tenants_dir / tenant.id / "trace.yaml")
    return TraceConfig.model_validate(
        {
            "roots": [*own.get("roots", []), *pack.get("roots", [])],
            "alerts": [*pack.get("alerts", []), *own.get("alerts", [])],
            "show": {**pack.get("show", {}), **own.get("show", {})},
            "value_names": {**pack.get("value_names", {}), **own.get("value_names", {})},
        }
    )


def _read_perm(onto: Ontology, t: str) -> str:
    return f"{onto.objects[t].domain}.{t}:read"


async def find_root(
    session: AsyncSession, onto: Ontology, cfg: TraceConfig, principal: Principal, text: str
) -> list[ObjectState]:
    """「追溯 XXX」：按 trace.yaml 的入口顺序找对象。先按编号，再按各入口的属性。"""
    from fos.entry.draft import find_objects  # 避免循环导入

    text = text.strip()
    for r in cfg.roots:
        if not principal.can(_read_perm(onto, r.object)):
            continue
        hits = await find_objects(session, principal.tenant_id, r.object, text, r.keys, "mock")
        if hits:
            return hits
    return []


async def _links(session: AsyncSession, tenant: str, nodes: list[tuple[str, str]], *, outgoing: bool):
    if not nodes:
        return []
    if outgoing:
        conds = [and_(ObjectLink.from_type == t, ObjectLink.from_id == i) for t, i in nodes]
    else:
        conds = [and_(ObjectLink.to_type == t, ObjectLink.to_id == i) for t, i in nodes]
    q = select(ObjectLink).where(ObjectLink.tenant_id == tenant, or_(*conds))
    return (await session.scalars(q)).all()


async def trace_report(
    session: AsyncSession,
    onto: Ontology,
    cfg: TraceConfig,
    principal: Principal,
    object_type: str,
    object_id: str,
    *,
    depth: int = 3,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    tenant = principal.tenant_id
    root = (object_type, object_id)
    role: dict[tuple[str, str], tuple[str, int, str]] = {root: ("root", 0, "")}  # 节点 → (方向, 距离, 经由)
    edges: set[tuple[str, str, str, str, str]] = set()

    def rel_name(link: ObjectLink) -> str:
        o = onto.objects.get(link.from_type)
        p = o.properties.get(link.rel) if o else None
        return p.name if p else link.rel

    async def walk(direction: str, outgoing: bool, start: list[tuple[str, str]], max_d: int) -> None:
        frontier = start
        for d in range(1, max_d + 1):
            rows = await _links(session, tenant, frontier, outgoing=outgoing)
            nxt = []
            for link in rows:
                edges.add((link.from_type, link.from_id, link.rel, link.to_type, link.to_id))
                node = (link.to_type, link.to_id) if outgoing else (link.from_type, link.from_id)
                if node not in role and len(role) < MAX_NODES:
                    role[node] = (direction, d, rel_name(link))
                    nxt.append(node)
            frontier = nxt
            if not frontier:
                break

    await walk("upstream", True, [root], depth)
    await walk("downstream", False, [root], depth)
    # 旁证：上游节点被谁引用（来料检验、同一批原料 / 同一窑次的其他批次）
    ups = [n for n, (r, _, _) in role.items() if r == "upstream"]
    rows = await _links(session, tenant, ups, outgoing=False)
    for link in rows:
        edges.add((link.from_type, link.from_id, link.rel, link.to_type, link.to_id))
        node = (link.from_type, link.from_id)
        if node not in role and len(role) < MAX_NODES:
            role[node] = ("related", role[(link.to_type, link.to_id)][1] + 1, rel_name(link))

    nodes = []
    alerts = []
    for (t, i), (direction, dist, via) in role.items():
        known = t in onto.objects
        readable = known and principal.can(_read_perm(onto, t))
        obj = await session.get(ObjectState, (tenant, t, i))
        n: dict[str, Any] = {
            "object_type": t,
            "type_name": onto.objects[t].name if known else t,
            "object_id": i,
            "direction": direction,
            "distance": dist,
            "via": via,
            "data_mode": obj.data_mode if obj else None,
            "state": obj.state if (obj and readable) else None,
            "redacted": not readable,
            "alerts": [],
        }
        if readable and obj:
            for a in cfg.alerts:
                if a.object == t and obj.state.get(a.field) in a.values:
                    n["alerts"].append(a.text)
                    alerts.append({"object_type": t, "object_id": i, "text": a.text})
        nodes.append(n)
    order = {"root": 0, "upstream": 1, "downstream": 2, "related": 3}
    nodes.sort(key=lambda n: (order[n["direction"]], n["distance"], n["object_type"], n["object_id"]))
    keep = set(role)
    return {
        "root": {"object_type": object_type, "object_id": object_id},
        "alerts": alerts,
        "nodes": nodes,
        "edges": [
            {"from_type": a, "from_id": b, "rel": r, "to_type": c, "to_id": e}
            for a, b, r, c, e in sorted(edges)
            if (a, b) in keep and (c, e) in keep
        ],
        "data_mode": "mock" if any(n["data_mode"] == "mock" for n in nodes) else "real",
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# ---------------- 给人看 ----------------


def _fmt(onto: Ontology, cfg: TraceConfig, t: str, k: str, v: Any, labels: dict[tuple[str, str], str]) -> str:
    p = onto.objects[t].properties[k]
    if p.type == "ref":
        ids = v if isinstance(v, list) else [v]
        return "、".join(labels.get((p.target or "", x), x) for x in ids)
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, str) and p.type == "datetime":
        v = v[:16].replace("T", " ")
    s = cfg.value_names.get(str(v), str(v))
    if p.type in ("number", "integer"):
        return f"{p.name} {s}{p.unit or ''}"
    return s


def node_label(n: dict[str, Any]) -> str:
    s = n["state"] or {}
    name = s.get("name") or s.get("code") or s.get("run_no")
    return f"{name}（{n['object_id']}）" if name and name != n["object_id"] else n["object_id"]


def describe_node(onto: Ontology, cfg: TraceConfig, n: dict[str, Any], labels) -> str:
    if n["redacted"]:
        return f"{n['object_id']}：有关联记录，但你没有查看权限"
    s = n["state"] or {}
    t = n["object_type"]
    keys = cfg.show.get(t) or [k for k in onto.objects[t].properties][:3]
    label = node_label(n)
    parts = [
        _fmt(onto, cfg, t, k, s[k], labels)
        for k in keys
        if s.get(k) not in (None, "", []) and not (isinstance(s[k], str) and s[k] in label)
    ]
    head = "⚠" + "、".join(n["alerts"]) + " " if n["alerts"] else ""
    return f"{head}{label}" + (f" {' / '.join(parts)}" if parts else "")


def report_lines(onto: Ontology, cfg: TraceConfig, report: dict[str, Any]) -> list[tuple[str, str]]:
    """[(分组, 一行说明)]：分组 = 方向 + 对象类型，告警在前。"""
    labels = {(n["object_type"], n["object_id"]): node_label(n) for n in report["nodes"] if not n["redacted"]}
    prefix = {"upstream": "", "downstream": "下游·", "related": "关联·"}
    out = []
    for n in report["nodes"]:
        if n["direction"] == "root":
            continue
        out.append((prefix[n["direction"]] + n["type_name"], describe_node(onto, cfg, n, labels)))
    return out


def report_card(onto: Ontology, cfg: TraceConfig, report: dict[str, Any]) -> Card:
    root = report["nodes"][0]
    labels = {(n["object_type"], n["object_id"]): node_label(n) for n in report["nodes"] if not n["redacted"]}
    groups: dict[str, list[str]] = {}
    for g, line in report_lines(onto, cfg, report):
        groups.setdefault(g, []).append(line)
    fields = [CardField(label=g[:20], value="；".join(lines)[:200]) for g, lines in list(groups.items())[:12]]
    if report["alerts"]:
        text = "**发现异常**：" + "；".join(f"{a['object_id']} {a['text']}" for a in report["alerts"][:5])
    elif len(report["nodes"]) == 1:
        text = "没有找到关联记录（原料、窑次、检验都还没有登记）。"
    else:
        text = "上下游记录如下，没有发现异常。"
    text += f"\n{describe_node(onto, cfg, root, labels)}"
    return Card(
        title=f"追溯：{root['type_name']} {root['object_id']}"[:40],
        text=text[:500],
        fields=fields,
        data_mode=report["data_mode"],
        footer=f"共 {len(report['nodes'])} 条记录 · 用时 {report['elapsed_ms']} ms · 只读查询",
    )
