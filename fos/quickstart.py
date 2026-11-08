"""本地试用与首次部署：建表、建管理员并发 Key、生成样例文件。

fos quickstart                 建表 + 建 luneng 管理员 + 打印 API Key
fos serve                      启动网页与接口（http://localhost:8000/app/）
fos samples ./samples          生成三份样例文件（技术要求、招标文件、工程量清单），不需要任何模型即可试用
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

from sqlalchemy import select

from fos.config import Settings
from fos.db.models import Base, User
from fos.db.session import make_engine, make_sessionmaker
from fos.governance import apikeys
from fos.tenancy import TenantRegistry


async def init_db(s: Settings) -> str:
    """SQLite（本地试用）直接建表；PostgreSQL 走 Alembic 迁移。"""
    if s.database_url.startswith("sqlite"):
        path = s.database_url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(s.database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()
        return "SQLite：已建表（仅供本地试用，正式环境用 PostgreSQL）"
    ini = Path("alembic.ini")
    if not ini.exists():
        raise SystemExit("找不到 alembic.ini：请在仓库根目录（或镜像的 /app）下运行")
    from alembic import command
    from alembic.config import Config

    # alembic 的 env.py 自己调用 asyncio.run()，放到独立线程里跑
    await asyncio.to_thread(command.upgrade, Config(str(ini)), "head")
    return "PostgreSQL：已执行 alembic upgrade head"


async def quickstart(
    s: Settings, tenant_id: str, login: str, display_name: str, *, new_key: bool = False
) -> int:
    """可重复执行：用户已存在时只在 new_key=True 时再发一把 Key（部署脚本每次都会调用）。"""
    registry = TenantRegistry.load(s.tenants_dir)
    tenant = registry.get(tenant_id)
    if tenant is None:
        print(f"租户 {tenant_id} 不存在（tenants/ 下有：{', '.join(registry.ids())}）", file=sys.stderr)
        return 1
    print(await init_db(s))
    engine = make_engine(s.database_url)
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            u = (
                await session.execute(select(User).where(User.tenant_id == tenant_id, User.login == login))
            ).scalar_one_or_none()
            if u is None:
                u = User(tenant_id=tenant_id, login=login, display_name=display_name, roles=["admin"])
                session.add(u)
                await session.flush()
                print(f"已创建用户 {login}（{tenant.name}，角色 admin）")
            elif not new_key:
                print(f"用户 {login} 已存在，不再发新 Key（需要新 Key：fos issue-key {tenant_id} {login}）")
                return 0
            k = await apikeys.issue(session, u, label="quickstart")
    finally:
        await engine.dispose()
    print("\nAPI Key（只显示这一次，请妥善保存）：")
    print(k.plaintext)
    print("\n下一步：打开网页 /app/ 用上面的 Key 登录（本机试用先执行 fos serve）")
    return 0


# ---------------- 样例文件（虚构内容，用于试用与演示） ----------------


def sample_tech_docx() -> bytes:
    from docx import Document

    d = Document()
    d.add_heading("2# 辊道窑烧成带炉衬改造 技术要求", level=1)
    d.add_paragraph("文件编号：JS-2026-117（样例，内容为虚构）")
    d.add_heading("1 项目概况", level=2)
    for line in [
        "客户名称：安徽某陶瓷有限公司",
        "项目名称：2#辊道窑烧成带炉衬改造",
        "项目地点：安徽省六安市",
        "交货期：合同签订后 30 天内到货",
        "交货方式：送货到现场，卸车由需方负责",
    ]:
        d.add_paragraph(line)
    d.add_heading("2 工况条件", level=2)
    for line in [
        "2.1 烧成带正常工作温度 1250℃，最高温度 1300℃。",
        "2.2 炉内气氛：氧化气氛，局部弱还原。",
        "2.3 升温速率不大于 150℃/h，每 45 分钟一个烧成周期，冷热交替频繁。",
        "2.4 外壁温度不高于 60℃（环境温度 25℃）。",
        "2.5 炉膛尺寸：36 m × 2.4 m × 0.9 m（长 × 宽 × 高）。",
    ]:
        d.add_paragraph(line)
    d.add_heading("3 衬里要求", level=2)
    for line in [
        "衬里结构：工作层 + 隔热层 + 保温层，三层复合。",
        "工作层：JM-26 莫来石轻质砖 230 mm。",
        "隔热层：LN-1260 硅酸铝纤维板 100 mm。",
        "总厚度：不小于 350 mm。",
        "锚固方式：耐热钢 Y 形锚固件，间距 300 mm。",
    ]:
        d.add_paragraph(line)
    d.add_heading("4 工程量", level=2)
    t = d.add_table(rows=4, cols=4)
    rows = [
        ["序号", "部位", "面积", "单位"],
        ["1", "炉墙（两侧）", "172.8", "m²"],
        ["2", "炉顶", "100.8", "m²"],
        ["3", "端墙", "4.3", "m²"],
    ]
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    d.add_heading("5 标准与验收", level=2)
    for line in [
        "执行标准：GB 50211-2014《工业炉砌筑工程施工与验收规范》、GB/T 3994-2013《粘土质隔热耐火砖》。",
        "检测要求：每批次提供体积密度、导热系数、加热永久线变化检测报告。",
        "质保期：一年（自验收合格之日起）。",
    ]:
        d.add_paragraph(line)
    d.add_heading("6 商务", level=2)
    for line in [
        "付款方式：预付 30%，到货验收后付 60%，质保金 10%。",
        "税率：13%",
        "报价有效期：30 天",
        "包装要求：木托盘加缠绕膜，纤维制品防潮包装。",
    ]:
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def sample_tender_docx() -> bytes:
    from docx import Document

    d = Document()
    d.add_heading("某钢铁公司加热炉耐火材料采购项目 招标文件", level=1)
    d.add_paragraph("（样例，内容为虚构）")
    d.add_heading("第一章 招标公告", level=2)
    for line in [
        "招标项目名称：1# 加热炉耐火材料采购",
        "招标编号：ZB-2026-0458",
        "招标人：某钢铁股份有限公司",
        "招标代理机构：某招标咨询有限公司",
        "最高投标限价：186.5 万元",
        "交货期：合同签订后 45 日历天",
        "交货地点：招标人厂区指定仓库",
        "投标截止时间：2026年12月18日 09:30",
        "开标时间：2026年12月18日 09:30",
        "澄清截止时间：2026年12月8日 17:00",
        "投标保证金：3 万元",
        "联系人：王工",
        "联系电话：0564-3312345",
    ]:
        d.add_paragraph(line)
    d.add_heading("第二章 投标人须知", level=2)
    for line in [
        "投标人资格要求：具有独立法人资格，近三年具有不少于 2 项加热炉耐火材料供货业绩。",
        "★ 投标人必须提供所投产品近一年内的第三方检测报告。",
        "★ 投标产品分类温度不得低于 1400℃。",
        "▲ 质保期不少于 12 个月。",
        "投标文件未按要求密封的，按无效投标处理。",
        "未提交投标保证金的，其投标将被否决其投标。",
    ]:
        d.add_paragraph(line)
    d.add_heading("第三章 评标办法", level=2)
    for line in [
        "评标办法：综合评分法。技术分 50 分，商务分 20 分，价格分 30 分。",
        "业绩：每提供 1 项加热炉耐火材料供货业绩得 2 分，最多 10 分。",
        "检测报告：提供所投产品第三方检测报告且指标满足技术规格的得 5 分。",
        "质量管理体系：具有 ISO9001 质量管理体系认证证书的得 3 分。",
    ]:
        d.add_paragraph(line)
    d.add_heading("第四章 合同条款", level=2)
    d.add_paragraph("付款方式：到货验收合格后支付 90%，质保期满无质量问题支付 10%。")
    d.add_paragraph("质保期：12 个月。")
    d.add_paragraph("卖方逾期交货的，每逾期一日按合同总价的 0.5% 支付违约金。")
    d.add_heading("第五章 技术规格书", level=2)
    for line in [
        "5.1 设备：1# 步进式加热炉，炉墙、炉顶耐火材料供货。",
        "5.2 正常工作温度：1250℃；最高温度：1320℃。",
        "5.3 炉内气氛：氧化气氛。",
        "5.4 外壁温度不高于 80℃（环境温度 30℃）。",
        "5.5 衬里总厚度：不大于 300 mm。",
        "▲ 5.6 纤维制品体积密度允许偏差不超过 ±10%。",
        "5.7 每批次产品须提供出厂检验报告。",
        "5.8 施工与验收执行 GB 50211-2014《工业炉砌筑工程施工与验收规范》。",
    ]:
        d.add_paragraph(line)
    t = d.add_table(rows=3, cols=4)
    for r, row in enumerate(
        [["序号", "部位", "面积", "单位"], ["1", "炉墙", "120", "m²"], ["2", "炉顶", "60", "m²"]]
    ):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    d.add_heading("第六章 投标文件格式", level=2)
    for line in [
        "投标文件应包括：投标函、法定代表人授权书、营业执照副本复印件、ISO9001 质量管理体系认证证书、"
        "近三年业绩合同复印件、第三方检测报告、投标保证金缴纳凭证。",
        "投标文件正本一份、副本四份，须逐页签字盖章。",
    ]:
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def sample_boq_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "工程量清单"
    ws.append(["序号", "部位", "材料", "厚度(mm)", "面积", "单位"])
    ws.append([1, "炉墙", "JM-26 莫来石轻质砖", 230, 172.8, "m²"])
    ws.append([2, "炉墙", "LN-1260 硅酸铝纤维板", 100, 172.8, "m²"])
    ws.append([3, "炉顶", "LN-1430 含锆纤维模块", 250, 100.8, "m²"])
    ws.append([4, "端墙", "JM-26 莫来石轻质砖", 230, 4.3, "m²"])
    wb.create_sheet("说明").append(["工作温度（℃）", 1250])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def write_samples(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "样例-技术要求-2#辊道窑.docx": sample_tech_docx(),
        "样例-招标文件-加热炉耐材.docx": sample_tender_docx(),
        "样例-工程量清单.xlsx": sample_boq_xlsx(),
    }
    paths = []
    for name, data in files.items():
        p = out / name
        p.write_bytes(data)
        paths.append(p)
    return paths


# ---------------- OCR 连通性自检 ----------------


def _test_image() -> bytes:
    """画一张带文字和表格线的测试图（只用 ASCII，避免依赖中文字体）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (900, 360), "white")
    d = ImageDraw.Draw(img)
    lines = [
        "Technical requirement JS-2026-117",
        "2.1 Normal working temperature 1250 C, max 1300 C",
        "2.4 Shell temperature <= 60 C",
    ]
    for i, t in enumerate(lines):
        d.text((40, 30 + 40 * i), t, fill="black", font_size=26)
    y0 = 170
    for r in range(4):
        d.line((40, y0 + 40 * r, 640, y0 + 40 * r), fill="black", width=2)
    for x in (40, 240, 440, 640):
        d.line((x, y0, x, y0 + 120), fill="black", width=2)
    for r, row in enumerate([["Part", "Area", "Unit"], ["Wall", "172.8", "m2"], ["Roof", "100.8", "m2"]]):
        for c, v in enumerate(row):
            d.text((55 + 200 * c, y0 + 8 + 40 * r), v, fill="black", font_size=24)
    import io as _io

    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def ocr_check(s: Settings, image: Path | None = None) -> int:
    """对当前配置的 OCR 服务发一张图，打印耗时和识别结果。部署后第一件事就跑它。"""
    import time

    from fos.perception.ocr import OCRError, make_ocr

    ocr = make_ocr(s)
    base = getattr(getattr(ocr, "_http", None), "base_url", "")
    print(f"OCR 模式：{s.ocr_mode.value}  服务：{base or '（不调用）'}  模型：{getattr(ocr, '_model', '-')}")
    data = image.read_bytes() if image else _test_image()
    if image and not data.startswith(b"\x89PNG"):
        from PIL import Image

        buf = io.BytesIO()
        Image.open(io.BytesIO(data)).convert("RGB").save(buf, format="PNG")
        data = buf.getvalue()
    t0 = time.perf_counter()
    try:
        md = await ocr.recognize(data, page=1)
    except OCRError as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    finally:
        await ocr.aclose()
    print(f"耗时 {time.perf_counter() - t0:.1f} 秒，data_mode={ocr.data_mode}\n---")
    print(md)
    if ocr.data_mode == "mock":
        print("---\n当前是模拟模式（FOS_OCR_MODE=mock），上面不是真实识别结果。")
    return 0
