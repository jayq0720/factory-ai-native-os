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


class OCRMode(StrEnum):
    mock = "mock"  # 返回预置或占位结果，结果标记 data_mode=mock
    xiaomi = "xiaomi"  # 调用 vLLM / SGLang / Ollama 上部署的 Xiaomi-OCR-0（OpenAI 兼容接口）
    qwen_vl = "qwen_vl"  # 阿里云百炼 Qwen-VL-OCR（OpenAI 兼容接口，需要百炼 API Key）


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOS_", env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://fos:fos@localhost:5432/fos"
    tenants_dir: Path = Field(default=Path("tenants"))
    packs_dir: Path = Field(default=Path("packs"))

    llm_mode: LLMMode = LLMMode.mock
    litellm_base_url: str = "http://localhost:4000"
    litellm_api_key: str = ""  # LiteLLM master key，从 KMS / 凭据管家注入
    llm_timeout_s: float = 60.0

    # 文档解析
    blob_dir: Path = Field(default=Path("var/blobs"))  # 本地原件存储；生产换 OSS
    ocr_mode: OCRMode = OCRMode.mock
    ocr_base_url: str = ""  # 留空按模式取默认：xiaomi → http://localhost:8001/v1，qwen_vl → 百炼
    ocr_api_key: str = ""  # qwen_vl 填百炼 API Key；本地 vLLM 一般不需要
    ocr_model: str = ""  # 留空按模式取默认：xiaomi-ocr-0（vLLM --served-model-name）/ qwen-vl-ocr-latest
    ocr_timeout_s: float = 120.0
    ocr_max_tokens: int = 4096  # 单页输出上限（Xiaomi-OCR-0 上下文 16384）
    ocr_retries: int = 2  # 超时 / 5xx / 429 时重试次数
    ocr_concurrency: int = 4  # 同一份文档同时识别的页数
    ocr_render_scale: float = 2.0  # PDF 扫描页渲染倍率（72 dpi × 2 ≈ 144 dpi）
    max_upload_mb: int = 50
    max_pages: int = 300
    # 字段抽取：规则抽取总是运行；FOS_LLM_MODE=litellm 时再用模型补充（结果必须能在原文里找到）
    extract_with_llm: bool = True
    extract_chunk_chars: int = 6000

    # fos-edge 命令签名私钥（Ed25519，32 字节种子的 base64，或 PEM 全文）；只从环境变量 / KMS 注入。
    # 留空则不能下发命令（厂端也拒绝一切未签名命令）。生成：fos edge-keygen
    edge_command_key: str = ""

    # Agent 运行预算（方案 2.6 Budget Lease）：单次运行的上限
    agent_max_tokens: int = 40_000
    agent_tool_result_chars: int = 4_000  # 工具结果放进上下文的上限，超出部分只存在运行记录里（Spill）


@lru_cache
def get_settings() -> Settings:
    return Settings()
