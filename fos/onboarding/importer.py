"""主数据批量导入（W13 试点上线）：Excel 模板 → 预览 → 确认入账。

六安绿能没有 ERP，上线第一步是把供应商、客户、物料、产品、设备、窑炉录进系统。逐条在 IM 里录太慢，
所以给一份按本体生成的 Excel 模板，填好后：

1. 预览：整份文件在一个保存点里试着入账，逐行给出“新增 / 已存在 / 错误”，然后全部回滚；
2. 入账：没有任何错误才提交，一次全部入账（有错就一条也不写）。

每一行都经 Ledger.append()，以导入人的身份和权限写入（权限、本体校验、引用检查、Policy 都照常生效），
证据 = 导入文件原件 + 工作表 + 行号。同一对象重复导入按 key 判断“已存在”跳过，可以放心重复执行。
数值、日期都是确定性转换，不调用模型。
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from fos.entry.draft import OBJECT_ID, find_objects, norm
from fos.governance.principal import Principal
from fos.ledger import AppendRequest, ApprovalRequired, Evidence, Ledger, LedgerError
from fos.onboarding.config import ID_COLUMN, Column, ImportConfig, SheetDef
from fos.ontology import Ontology

MAX_ROWS = 5000
TRUE_WORDS = {"是", "有", "true", "yes", "y", "1", "√"}
FALSE_WORDS = {"否", "无", "false", "no", "n", "0", "×"}
LIST_SPLIT = re.compile(r"[、,，;；\n]+")
DATE_CN = re.compile(r"^(\d{4})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?$")

RowStatus = Literal["created", "exists", "error"]


class ImportFileError(ValueError):
    """整份文件不能导入（不是 Excel、没有认得的工作表……）。"""


@dataclass
class RowResult:
    row: int  # Excel 行号
    status: RowStatus
    object_id: str | None = None
    label: str = ""
    message: str = ""

    def out(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


@dataclass
class SheetResult:
    id: str
    name: str
    rows: list[RowResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # 整张表的问题（缺必填列……）

    def count(self, status: RowStatus) -> int:
        return sum(1 for r in self.rows if r.status == status)


@dataclass
class ImportReport:
    filename: str
    sha256: str
    committed: bool = False
    sheets: list[SheetResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(s.errors or s.count("error") for s in self.sheets)

    def out(self) -> dict[str, Any]:
        total = {st: sum(s.count(st) for s in self.sheets) for st in ("created", "exists", "error")}
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "ok": self.ok,
            "committed": self.committed,
            "counts": total,
            "warnings": self.warnings,
            "sheets": [
                {
                    "id": s.id,
                    "name": s.name,
                    "errors": s.errors,
                    "counts": {st: s.count(st) for st in ("created", "exists", "error")},
                    "rows": [r.out() for r in s.rows],
                }
                for s in self.sheets
            ],
        }


# ---------------- 模板 ----------------


def _hint(cfg: ImportConfig, onto: Ontology, c: Column) -> str:
    p = c.prop
    parts = [f"{'必填' if c.required else '选填'}"]
    match p.type:
        case "enum":
            opts = [cfg.value_names.get(v, v) for v in p.values or []]
            parts.append("可选：" + "、".join(opts))
        case "ref":
            target = onto.objects[p.target or ""]
            ks = cfg.key_sheet(onto, p.target or "")
            by = target.properties[ks.key].name if ks else "编号"
            parts.append(f"填{target.name}的{by}或编号" + ("，多个用顿号分隔" if p.many else ""))
        case "number" | "integer":
            parts.append("数字" + (f"，单位 {p.unit}" if p.unit else ""))
        case "date":
            parts.append("日期，如 2026-11-07")
        case "datetime":
            parts.append("日期时间，如 2026-11-07 08:00（按工厂时区）")
        case "bool":
            parts.append("填 是 / 否")
    return "；".join(parts)


def build_template(cfg: ImportConfig, onto: Ontology, tenant_name: str) -> bytes:
    """按本体生成导入模板：一张说明页 + 每类主数据一张表（表头带批注提示）。"""
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    info = wb.active
    info.title = "说明"
    lines = [
        f"{tenant_name} 主数据导入模板",
        "",
        "1. 每张工作表一类主数据，按工作表顺序导入；被引用的（如设备）放在引用它的（如窑炉）前面。",
        "2. 第一行是表头，不要改；带 * 的列必填。鼠标停在表头上可以看到填写说明。",
        f"3. “{ID_COLUMN}”列可以留空，系统自动编号；填了就用你的编号（字母、数字、-、_、.）。",
        "4. 引用其他主数据（如窑炉的“对应设备台账”）填对方的名称、牌号或编号，可以引用本文件前面表里的行。",
        "5. 先点“预览”看每一行的结果，没有错误再点“入账”；有任何错误就一条也不会写入。",
        "6. 已经存在的（名称 / 牌号相同）会跳过，重复导入同一份文件是安全的。",
        "7. 用不到的工作表可以删掉或留空。",
    ]
    for i, ln in enumerate(lines, 1):
        info.cell(row=i, column=1, value=ln)
    info["A1"].font = Font(bold=True, size=14)
    info.column_dimensions["A"].width = 100
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    req_fill = PatternFill("solid", fgColor="FCE4D6")
    for s in cfg.sheets:
        ws = wb.create_sheet(s.name)
        cols = cfg.columns(onto, s)
        ws.cell(row=1, column=1, value=ID_COLUMN).comment = Comment("选填；留空自动编号", "fos")
        ws.column_dimensions["A"].width = 14
        for j, c in enumerate(cols, 2):
            cell = ws.cell(row=1, column=j, value=c.header)
            cell.font = Font(bold=True)
            cell.fill = req_fill if c.required else head_fill
            cell.comment = Comment(_hint(cfg, onto, c), "fos")
            ws.column_dimensions[cell.column_letter].width = max(12, len(c.header) * 2 + 4)
        ws.cell(row=1, column=1).font = Font(bold=True)
        ws.cell(row=1, column=1).fill = head_fill
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------- 读文件 ----------------


def _header(v: Any) -> str:
    return re.sub(r"[\s*＊]+", "", str(v or ""))


def read_workbook(data: bytes) -> dict[str, list[tuple[int, list[Any]]]]:
    """工作表名 → [(行号, 单元格值)]，第一项是表头。"""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # openpyxl 对非 xlsx 抛各种异常
        raise ImportFileError("不是有效的 Excel（.xlsx）文件") from e
    out: dict[str, list[tuple[int, list[Any]]]] = {}
    try:
        for ws in wb.worksheets:
            rows: list[tuple[int, list[Any]]] = []
            for i, row in enumerate(ws.iter_rows(values_only=True), 1):
                if i > MAX_ROWS + 1:
                    raise ImportFileError(f"工作表“{ws.title}”超过 {MAX_ROWS} 行，请拆成几份导入")
                rows.append((i, list(row)))
            out[ws.title.strip()] = rows
    finally:
        wb.close()
    return out


# ---------------- 单元格 → 本体值（确定性转换） ----------------


class CellError(ValueError):
    pass


def _text(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="minutes")
    if isinstance(v, date):
        return v.isoformat()
    return str(v).strip()


def _number(v: Any) -> float:
    if isinstance(v, bool):
        raise CellError("应为数字")
    if isinstance(v, int | float):
        return float(v)
    s = unicodedata_nfkc(str(v)).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        raise CellError(f"“{v}”不是数字") from None


def unicodedata_nfkc(s: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKC", s)


def _date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = unicodedata_nfkc(str(v)).strip()
    m = DATE_CN.match(s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    raise CellError(f"“{v}”不是日期（如 2026-11-07）")


def _datetime(v: Any, tz: ZoneInfo) -> str:
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, date):
        dt = datetime.combine(v, time())
    else:
        s = unicodedata_nfkc(str(v)).strip().replace("/", "-")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            dt = datetime.combine(_date(s), time())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.isoformat()


def _enum(v: Any, values: list[str], names: dict[str, str]) -> str:
    s = _text(v)
    if s in values:
        return s
    by_name = {names.get(x, x): x for x in values}
    if s in by_name:
        return by_name[s]
    raise CellError(f"“{s}”不是可选值（{'、'.join(by_name)}）")


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = _text(v).lower()
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    raise CellError(f"“{v}”应填 是 / 否")


# ---------------- 导入 ----------------


@dataclass
class _Ctx:
    session: AsyncSession
    ledger: Ledger
    principal: Principal
    onto: Ontology
    cfg: ImportConfig
    tz: ZoneInfo
    evidence_ref: str
    filename: str


async def _resolve_ref(ctx: _Ctx, target: str, text: str) -> str:
    ks = ctx.cfg.key_sheet(ctx.onto, target)
    hits = await find_objects(
        ctx.session, ctx.principal.tenant_id, target, text, [ks.key] if ks else [], "real"
    )
    name = ctx.onto.objects[target].name
    if not hits:
        raise CellError(f"找不到{name}“{text}”（先导入它，或放在本表前面的工作表里）")
    if len(hits) > 1:
        raise CellError(f"{name}“{text}”对应多条记录，请填编号")
    return hits[0].object_id


async def _convert(ctx: _Ctx, c: Column, v: Any) -> Any:
    p = c.prop
    match p.type:
        case "string" | "text":
            return _text(v)
        case "number":
            return _number(v)
        case "integer":
            n = _number(v)
            if not n.is_integer():
                raise CellError(f"“{v}”应为整数")
            return int(n)
        case "bool":
            return _bool(v)
        case "date":
            return _date(v).isoformat()
        case "datetime":
            return _datetime(v, ctx.tz)
        case "enum":
            return _enum(v, p.values or [], ctx.cfg.value_names)
        case "ref":
            assert p.target
            if p.many:
                parts = [x.strip() for x in LIST_SPLIT.split(_text(v)) if x.strip()]
                return [await _resolve_ref(ctx, p.target, x) for x in parts]
            return await _resolve_ref(ctx, p.target, _text(v))
    raise CellError(f"不支持的类型 {p.type}")  # pragma: no cover


def _idem(sheet: SheetDef, key_value: str) -> str:
    k = f"import:{sheet.id}:{norm(key_value)}"
    if len(k) > 128:
        k = f"import:{sheet.id}:#" + hashlib.sha256(norm(key_value).encode()).hexdigest()[:40]
    return k


async def _row(ctx: _Ctx, sheet: SheetDef, cols: dict[int, Column], id_col: int | None, rown: int, cells):
    obj_type = ctx.cfg.object_of(ctx.onto, sheet)
    values: dict[str, Any] = {}
    errs: list[str] = []
    bad: set[str] = set()
    for j, c in cols.items():
        v = cells[j] if j < len(cells) else None
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        try:
            values[c.field] = await _convert(ctx, c, v)
        except CellError as e:
            errs.append(f"{c.header.rstrip('*')}：{e}")
            bad.add(c.field)
    missing = [
        c.header.rstrip("*")
        for c in cols.values()
        if c.required and c.field not in values and c.field not in bad
    ]
    if missing:
        errs.append("缺少必填：" + "、".join(missing))
    oid = None
    if id_col is not None and id_col < len(cells) and cells[id_col] not in (None, ""):
        oid = _text(cells[id_col])
        if not OBJECT_ID.match(oid):
            errs.append(f"编号“{oid}”含有不能用的字符")
    key_value = str(values.get(sheet.key) or "")
    label = key_value or (oid or "")
    if errs:
        return RowResult(rown, "error", oid, label, "；".join(errs))

    # 已存在：按编号或 key 查（只看真实数据）
    hits = await find_objects(
        ctx.session, ctx.principal.tenant_id, obj_type, oid or key_value, [] if oid else [sheet.key], "real"
    )
    if not hits and oid:
        hits = await find_objects(
            ctx.session, ctx.principal.tenant_id, obj_type, key_value, [sheet.key], "real"
        )
    if hits:
        return RowResult(rown, "exists", hits[0].object_id, label, "已存在，跳过")

    oid = oid or str(uuid.uuid4())
    evidence = [
        Evidence(
            kind="document",
            ref=ctx.evidence_ref,
            note=f"主数据导入 {ctx.filename[:100]} · {sheet.name} 第 {rown} 行",
        )
    ]
    try:
        async with ctx.session.begin_nested():
            for i, ev in enumerate(sheet.events):
                fields = ctx.onto.events[ev].fields
                payload = {k: values[k] for k in fields if k in values}
                if i > 0 and not payload:
                    continue
                await ctx.ledger.append(
                    ctx.session,
                    ctx.principal,
                    AppendRequest(
                        type=ev,
                        object_id=oid,
                        payload=payload,
                        evidence=evidence,
                        idempotency_key=_idem(sheet, key_value) if i == 0 else None,
                    ),
                )
    except ApprovalRequired as e:
        return RowResult(rown, "error", oid, label, f"{e}，不能批量导入，请单独提交")
    except LedgerError as e:
        return RowResult(rown, "error", oid, label, str(e))
    return RowResult(rown, "created", oid, label)


async def run_import(
    session: AsyncSession,
    *,
    ledger: Ledger,
    principal: Principal,
    onto: Ontology,
    cfg: ImportConfig,
    tz: ZoneInfo,
    data: bytes,
    filename: str,
    evidence_ref: str,
    commit: bool,
) -> ImportReport:
    """预览（commit=False）或入账。入账也只在整份文件没有错误时提交，否则回滚并返回报告。"""
    report = ImportReport(filename=filename, sha256=hashlib.sha256(data).hexdigest())
    book = read_workbook(data)
    by_name = {s.name: s for s in cfg.sheets}
    known = [n for n in book if n in by_name]
    if not known:
        raise ImportFileError("没有认得的工作表（请用“下载模板”生成的文件，工作表名不要改）")
    report.warnings += [f"工作表“{n}”不是主数据表，已忽略" for n in book if n not in by_name and n != "说明"]
    ctx = _Ctx(session, ledger, principal, onto, cfg, tz, evidence_ref, filename)
    outer = await session.begin_nested()
    try:
        for s in cfg.sheets:  # 按配置顺序，不按文件里的顺序
            rows = book.get(s.name)
            if not rows:
                continue
            res = SheetResult(s.id, s.name)
            report.sheets.append(res)
            obj = onto.objects[cfg.object_of(onto, s)]
            perm = onto.events[s.events[0]].permission
            if not principal.can(perm):
                res.errors.append(f"导入{obj.name}需要权限 {perm}")
                continue
            header = [_header(v) for v in rows[0][1]]
            columns = cfg.columns(onto, s)
            by_header = {c.header.rstrip("*"): c for c in columns} | {c.field: c for c in columns}
            cols: dict[int, Column] = {}
            id_col = None
            for j, h in enumerate(header):
                if h == ID_COLUMN:
                    id_col = j
                elif h in by_header:
                    cols[j] = by_header[h]
                elif h:
                    report.warnings.append(f"{s.name}：不认识的列“{h}”，已忽略")
            have = {c.field for c in cols.values()}
            lack = [c.header.rstrip("*") for c in columns if c.required and c.field not in have]
            if lack:
                res.errors.append("缺少必填列：" + "、".join(lack))
                continue
            for rown, cells in rows[1:]:
                if all(v is None or (isinstance(v, str) and not v.strip()) for v in cells):
                    continue
                res.rows.append(await _row(ctx, s, cols, id_col, rown, cells))
        if commit and report.ok:
            await outer.commit()
            report.committed = True
        else:
            await outer.rollback()
    except BaseException:
        if outer.is_active:
            await outer.rollback()
        raise
    return report
