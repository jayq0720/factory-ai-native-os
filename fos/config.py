"""进程级配置。只从环境变量 / .env 读取，密钥不落代码。"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMMode(StrEnum):
    mock = "mock"  # 预置结果，不调用任何模型（开发、演示、CI）
    litellm = "litellm"  # 通过 LiteLLM 代理调用百炼 / DeepSeek 等


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOS_", env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://fos:fos@localhost:5432/fos"
    tenants_dir: Path = Field(default=Path("tenants"))

    llm_mode: LLMMode = LLMMode.mock
    litellm_base_url: str = "http://localhost:4000"
    litellm_api_key: str = ""  # LiteLLM master key，从 KMS / 凭据管家注入
    llm_timeout_s: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
