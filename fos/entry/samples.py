"""拍照录单演示：一套虚构的主数据 + 五张单据照片，串起一条完整追溯链。

    原料送货单 → 生产任务单 → 装窑记录 → 报工单 → 理化检验报告

照片和对应的模拟 OCR 结果放在 packs/refractory/fixtures/（照片按像素内容匹配 OCR 样例，
所以不依赖任何模型服务）。主数据以模拟数据（data_mode=mock）写入，不能用于正式导出。

开发者改了单据内容后重新生成照片和样例：
    .venv/bin/python -m fos.entry.samples
"""

from __future__ import annotations

import io
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from fos.config import Settings
from fos.db.models import ObjectState, User
from fos.db.session import make_engine, make_sessionmaker
from fos.governance.principal import principal_for_user
from fos.ledger import AppendRequest, Ledger
from fos.ontology.loader import load_ontology
from fos.tenancy import TenantRegistry

PACK_DIR = Path(__file__).resolve().parents[2] / "packs" / "refractory" / "fixtures"
PHOTO_DIR = PACK_DIR / "entry"
OCR_DIR = PACK_DIR / "ocr"

# 主数据：(事件, 对象编号, payload)
MASTER: list[tuple[str, str, dict]] = [
    ("purchase.supplier_registered", "S-JY", {"name": "安徽金岩高岭土有限公司", "contact": "王经理"}),
    (
        "warehouse.material_defined",
        "M-MLS",
        {"name": "莫来石粉", "spec": "M60 200目", "category": "raw", "unit": "t"},
    ),
    ("sales.product_defined", "P-QG10", {"code": "QG-1.0", "name": "轻质莫来石隔热砖", "unit": "块"}),
    (
        "production.kiln_registered",
        "KILN-2",
        {"name": "2号隧道窑", "kiln_type": "tunnel", "access_path": "manual"},
    ),
    (
        "production.kiln_run_started",
        "KR-2-1107",
        {"kiln": "KILN-2", "run_no": "2-1107", "started_at": "2026-11-07T08:00:00+08:00", "status": "firing"},
    ),
]

# 单据：(文件名, 照片上的文字 = 模拟 OCR 结果)
SAMPLES: list[tuple[str, str]] = [
    (
        "1-送货单.png",
        "# 安徽金岩高岭土有限公司 送货单\n\n"
        "供货单位：安徽金岩高岭土有限公司\n\n收货单位：六安绿能\n\n"
        "品名：莫来石粉\n\n规格：M60 200目\n\n批号：JY261105\n\n"
        "数量：12.5 吨\n\n送货日期：2026年11月5日\n\n采购单号：PO-2611-003\n\n签收：",
    ),
    (
        "2-生产任务单.png",
        "# 生产任务单\n\n生产批号：B261106-01\n\n产品牌号：QG-1.0\n\n"
        "原料批号：JY261105\n\n计划数量：3000 块\n\n开工日期：2026年11月6日\n\n班组：成型一班",
    ),
    (
        "3-装窑记录.png",
        "# 装窑记录\n\n窑次号：2-1107\n\n生产批号：B261106-01\n\n装窑日期：2026年11月7日\n\n车数：12",
    ),
    (
        "4-报工单.png",
        "# 报工单\n\n生产批号：B261106-01\n\n合格数量：2870 块\n\n完工日期：2026年11月9日\n\n报工人：张师傅",
    ),
    (
        "5-检验报告.png",
        "# 理化性能检验报告\n\n报告编号：QC-2611-021\n\n生产批号：B261106-01\n\n检验日期：2026年11月10日\n\n"
        "体积密度：1.02 g/cm³\n\n常温耐压强度：4.6 MPa\n\n导热系数：0.38\n\n"
        "加热永久线变化：-0.6 %\n\nAl2O3：62.3 %\n\n检验结论：合格",
    ),
]


def _font(size: int):
    from PIL import ImageFont

    for p in (
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    raise SystemExit("没有找到中文字体，无法生成样例照片（照片已随仓库提供，一般不需要重新生成）")


def render(text: str) -> bytes:
    """把单据文字画成一张“照片”（白底黑字，略带纸张底色）。"""
    from PIL import Image, ImageDraw

    lines = [ln.lstrip("# ") for ln in text.split("\n") if ln.strip()]
    img = Image.new("RGB", (900, 120 + 56 * len(lines)), (250, 247, 238))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, img.width - 20, img.height - 20], outline=(90, 90, 90), width=2)
    title, body = _font(36), _font(28)
    for i, ln in enumerate(lines):
        d.text((60, 50 + 56 * i), ln, fill=(25, 25, 25), font=title if i == 0 else body)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def regenerate() -> None:
    from fos.perception.ocr import pixel_digest

    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in SAMPLES:
        png = render(text)
        (PHOTO_DIR / name).write_bytes(png)
        (OCR_DIR / f"{pixel_digest(png)}.md").write_text(text + "\n", encoding="utf-8")
        print(f"{name} → {pixel_digest(png)}.md")


def write_photos(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, _ in SAMPLES:
        p = out / name
        p.write_bytes((PHOTO_DIR / name).read_bytes())
        paths.append(p)
    return paths


async def seed_master(s: Settings, tenant_id: str, login: str) -> int:
    """以模拟数据写入演示主数据（可重复执行：已有的对象跳过）。"""
    registry = TenantRegistry.load(s.tenants_dir)
    tenant = registry.get(tenant_id)
    if tenant is None:
        print(f"租户 {tenant_id} 不存在", file=sys.stderr)
        return 1
    onto = load_ontology(tenant, packs_dir=s.packs_dir, tenants_dir=s.tenants_dir)
    ledger = Ledger(lambda _t: onto)
    engine = make_engine(s.database_url)
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            user = (
                await session.scalars(select(User).where(User.tenant_id == tenant_id, User.login == login))
            ).one_or_none()
            if user is None:
                print(f"用户 {login} 不存在（先执行 fos quickstart）", file=sys.stderr)
                return 1
            principal = principal_for_user(user, tenant)
            for ev, oid, payload in MASTER:
                obj = onto.events[ev].object
                if await session.get(ObjectState, (tenant_id, obj, oid)) is not None:
                    continue
                await ledger.append(
                    session,
                    principal,
                    AppendRequest(
                        type=ev,
                        object_id=oid,
                        payload=payload,
                        data_mode="mock",
                        idempotency_key=f"demo:{oid}",
                        occurred_at=datetime.now(UTC),
                    ),
                )
                print(f"已登记 {onto.objects[obj].name} {oid}（模拟数据）")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    regenerate()
