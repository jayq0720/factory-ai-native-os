from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fos.api.app import create_app
from fos.config import LLMMode, Settings
from fos.db.models import Base, User
from fos.db.session import make_engine, make_sessionmaker
from fos.governance import apikeys

REPO = Path(__file__).resolve().parents[1]


USERS = [
    ("admin", "luneng", ["admin"]),
    ("worker", "luneng", ["worker"]),
    ("warehouse", "luneng", ["warehouse"]),
    ("qc", "luneng", ["qc"]),
    ("shift_lead", "luneng", ["shift_lead"]),
    ("sales", "luneng", ["sales"]),
    ("rnd", "luneng", ["rnd"]),
    ("gm", "luneng", ["gm"]),
    ("gm2", "luneng", ["gm"]),
    ("purchaser", "luneng", ["purchaser"]),
    ("sales_lead", "luneng", ["sales_lead"]),
    ("other_admin", "other", ["admin"]),
]


@pytest.fixture
def tenants_dir(tmp_path: Path) -> Path:
    """真实的 luneng 配置 + 一个用于测试隔离的第二租户。"""
    d = tmp_path / "tenants"
    (d / "luneng").mkdir(parents=True)
    for name in ("tenant.yaml", "policy.yaml"):
        (d / "luneng" / name).write_text(
            (REPO / "tenants/luneng" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (d / "other").mkdir()
    (d / "other" / "tenant.yaml").write_text(
        textwrap.dedent(
            """
            id: other
            name: 另一家工厂
            industry_pack: refractory
            roles:
              admin: {name: 管理员, permissions: ["*.*:*"]}
            """
        ),
        encoding="utf-8",
    )
    return d


@pytest.fixture
def settings(tmp_path: Path, tenants_dir: Path) -> Settings:
    return Settings(
        # 设置 FOS_TEST_DATABASE_URL 时在真实 PostgreSQL 上跑（CI 这样做），否则用临时 SQLite
        database_url=os.environ.get("FOS_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'fos.db'}",
        tenants_dir=tenants_dir,
        packs_dir=REPO / "packs",
        blob_dir=tmp_path / "blobs",
        llm_mode=LLMMode.mock,
    )


@pytest.fixture
def keys(settings: Settings) -> dict[str, str]:
    """建表并发三把 key：luneng 管理员、luneng 工人、other 管理员。"""

    async def setup() -> dict[str, str]:
        engine = make_engine(settings.database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        out: dict[str, str] = {}
        async with sm() as s, s.begin():
            for name, tenant, roles in USERS:
                u = User(tenant_id=tenant, login=name, display_name=name, roles=roles, im_ids={})
                s.add(u)
                await s.flush()
                out[name] = (await apikeys.issue(s, u)).plaintext
        await engine.dispose()
        return out

    return asyncio.run(setup())


@pytest.fixture
def client(settings: Settings, keys: dict[str, str]):
    with TestClient(create_app(settings)) as c:
        yield c
