from __future__ import annotations

import asyncio
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


@pytest.fixture
def tenants_dir(tmp_path: Path) -> Path:
    """真实的 luneng 配置 + 一个用于测试隔离的第二租户。"""
    d = tmp_path / "tenants"
    (d / "luneng").mkdir(parents=True)
    (d / "luneng" / "tenant.yaml").write_text(
        (REPO / "tenants/luneng/tenant.yaml").read_text(encoding="utf-8"), encoding="utf-8"
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
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'fos.db'}",
        tenants_dir=tenants_dir,
        llm_mode=LLMMode.mock,
    )


@pytest.fixture
def keys(settings: Settings) -> dict[str, str]:
    """建表并发三把 key：luneng 管理员、luneng 工人、other 管理员。"""

    async def setup() -> dict[str, str]:
        engine = make_engine(settings.database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        out: dict[str, str] = {}
        async with sm() as s, s.begin():
            for name, tenant, roles in [
                ("admin", "luneng", ["admin"]),
                ("worker", "luneng", ["worker"]),
                ("other_admin", "other", ["admin"]),
            ]:
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
