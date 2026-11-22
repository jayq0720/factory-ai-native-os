"""新工厂租户一键零代码复制脚手架（W31）：生成租户目录与配置并即时完成本体/策略/Skill 校验。"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from fos.ops.service import verify_tenant_replication

TENANT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
ALLOWED_IM = {"dingtalk", "feishu", "wecom"}


def provision_tenant(
    *,
    packs_dir: Path,
    tenants_dir: Path,
    tenant_id: str,
    name: str,
    industry_pack: str = "refractory",
    im: list[str] | None = None,
    template_tenant: str | None = "luneng",
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """在 tenants_dir 下生成新工厂租户配置，并立即运行零代码复制就绪度验证。"""
    if not TENANT_ID_RE.match(tenant_id):
        raise ValueError(f"租户 ID 非法（需匹配 ^[a-z][a-z0-9-]{{1,30}}$）：{tenant_id}")
    if not name.strip():
        raise ValueError("工厂名称不能为空")
    pack_dir = packs_dir / industry_pack
    if not pack_dir.is_dir():
        raise ValueError(f"行业包不存在：{industry_pack}")
    im_list = im if im is not None else ["dingtalk"]
    bad_im = [x for x in im_list if x not in ALLOWED_IM]
    if bad_im:
        raise ValueError(f"不支持的 IM 信道：{bad_im}")

    target_dir = tenants_dir / tenant_id
    if target_dir.exists():
        raise ValueError(f"租户目录已存在，拒绝覆盖：{target_dir}")

    base_cfg: dict[str, Any]
    src_dir = (tenants_dir / template_tenant) if template_tenant else None
    if src_dir is not None and (src_dir / "tenant.yaml").exists():
        base_cfg = yaml.safe_load((src_dir / "tenant.yaml").read_text(encoding="utf-8")) or {}
    else:
        base_cfg = {
            "roles": {
                "admin": {"name": "系统管理员", "permissions": ["*.*:*"]},
                "gm": {"name": "总经理", "permissions": ["*.*:read", "*.*:approve"]},
            }
        }

    base_cfg["id"] = tenant_id
    base_cfg["name"] = name.strip()
    base_cfg["industry_pack"] = industry_pack
    base_cfg["timezone"] = timezone
    base_cfg["im"] = list(im_list)
    if "reports" in base_cfg and isinstance(base_cfg["reports"], dict):
        daily = base_cfg["reports"].get("daily")
        if isinstance(daily, dict):
            daily["to"] = []

    target_dir.mkdir(parents=True, exist_ok=False)
    try:
        (target_dir / "tenant.yaml").write_text(
            yaml.safe_dump(base_cfg, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        if src_dir is not None and (src_dir / "policy.yaml").exists():
            shutil.copy2(src_dir / "policy.yaml", target_dir / "policy.yaml")
        report = verify_tenant_replication(
            packs_dir=packs_dir,
            tenants_dir=tenants_dir,
            tenant_id=tenant_id,
        )
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise

    report["tenant_dir"] = str(target_dir)
    return report
