"""脱敏一键运维诊断包导出（零密钥、全量 SRE 排障快照 ZIP）。"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from fos.db.models import utcnow
from fos.eval.runner import run_golden_eval
from fos.governance.budget import check_tenant_budget
from fos.ops.doctor import run_doctor
from fos.ops.edge_fleet import edge_fleet_summary
from fos.ops.ledger_audit import verify_tenant_ledger
from fos.ops.metrics import query_audit_log
from fos.ops.packs import list_packs
from fos.ops.service import security_baseline

if TYPE_CHECKING:
    from fos.services import Services


def _encode_json(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")


async def build_support_bundle_zip(
    session: AsyncSession,
    svc: Services,
    tenant_id: str,
    *,
    audit_limit: int = 100,
) -> tuple[bytes, dict[str, Any]]:
    """打包单租户脱敏 SRE 排障快照 ZIP（不含任何 API Key 明文或外部凭据）。"""
    tenant = svc.registry.get(tenant_id)
    if tenant is None:
        raise ValueError(f"租户 {tenant_id} 不存在")

    try:
        eval_report: dict[str, Any] = run_golden_eval(svc, tenant_id)
    except ValueError as e:
        eval_report = {"skipped": True, "reason": str(e)}

    payloads: dict[str, Any] = {
        "doctor.json": await run_doctor(svc.settings, session=session, services=svc),
        "security.json": security_baseline(svc, tenant_id),
        "budget.json": await check_tenant_budget(session, tenant),
        "edge_fleet.json": await edge_fleet_summary(session, tenant_id),
        "packs.json": list_packs(svc.settings.packs_dir),
        "eval.json": eval_report,
        "ledger_check.json": await verify_tenant_ledger(session, tenant_id),
        "audit_recent.json": await query_audit_log(session, tenant_id, limit=max(1, min(audit_limit, 500))),
    }

    encoded: dict[str, bytes] = {name: _encode_json(data) for name, data in payloads.items()}
    file_entries = [
        {
            "name": name,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        for name, raw in sorted(encoded.items())
    ]
    manifest: dict[str, Any] = {
        "format": "fos-support-bundle-v1",
        "tenant_id": tenant_id,
        "tenant_name": tenant.name,
        "industry_pack": tenant.industry_pack,
        "env": svc.settings.env,
        "generated_at": utcnow().isoformat(),
        "files_count": len(file_entries),
        "files": file_entries,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", _encode_json(manifest))
        for name, raw in sorted(encoded.items()):
            zf.writestr(name, raw)

    return buf.getvalue(), manifest
