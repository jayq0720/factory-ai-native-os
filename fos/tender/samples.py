"""演示资质资料库（packs/<pack>/tender/sample_credentials.yaml）→ 账本事件（模拟数据）。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import yaml

from fos.ledger import AppendRequest


def credential_requests(
    path: Path, today: date, data_mode: Literal["real", "mock"] = "mock"
) -> list[AppendRequest]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for c in data.get("credentials", []):
        c = dict(c)
        cid = c.pop("id")
        if (n := c.pop("issued_days_ago", None)) is not None:
            c["issued_on"] = (today - timedelta(days=n)).isoformat()
        if (n := c.pop("valid_days", None)) is not None:
            c["valid_until"] = (today + timedelta(days=n)).isoformat()
        out.append(
            AppendRequest(
                type="kb.credential_registered",
                object_id=cid,
                payload=c,
                data_mode=data_mode,
                idempotency_key=f"kb-demo:{cid}",
            )
        )
    return out
