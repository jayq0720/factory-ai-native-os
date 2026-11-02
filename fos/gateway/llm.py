"""LLM 网关客户端。

业务代码只认模型别名（fos-chat / fos-reason / fos-fast / fos-vl / fos-embed），
别名到具体模型（百炼上的 DeepSeek、Qwen，DeepSeek 官方备用）的映射与故障切换
写在 deploy/litellm/config.yaml，由 LiteLLM 代理负责。换模型、换云、换本地推理
都只改那份配置，不改代码。

mock 模式不发任何网络请求，返回确定的占位结果并标记 data_mode="mock"，
用于开发、CI 和演示（方案 4.7：mock 与 real 不静默混用）。
"""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel

from fos.config import LLMMode, Settings

MODEL_ALIASES = ("fos-chat", "fos-reason", "fos-fast", "fos-vl")


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatResult(BaseModel):
    model: str
    content: str
    data_mode: Literal["mock", "real"]
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, mode: LLMMode, base_url: str = "", api_key: str = "", timeout_s: float = 60.0):
        self.mode = mode
        self._http: httpx.AsyncClient | None = None
        if mode is LLMMode.litellm:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            self._http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout_s)

    @classmethod
    def from_settings(cls, s: Settings) -> LLMClient:
        return cls(s.llm_mode, s.litellm_base_url, s.litellm_api_key, s.llm_timeout_s)

    async def aclose(self) -> None:
        if self._http:
            await self._http.aclose()

    async def chat(
        self, messages: list[ChatMessage], *, model: str = "fos-chat", tenant_id: str
    ) -> ChatResult:
        if model not in MODEL_ALIASES:
            raise LLMError(f"未知模型别名 {model!r}，可用：{', '.join(MODEL_ALIASES)}")
        if self.mode is LLMMode.mock:
            last = next((m.content for m in reversed(messages) if m.role == "user"), "")
            return ChatResult(model=model, content=f"[模拟回复] 收到：{last[:200]}", data_mode="mock")

        assert self._http is not None
        payload = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            # LiteLLM 按 user / metadata 统计每个租户的 token 与成本
            "user": tenant_id,
            "metadata": {"tenant_id": tenant_id},
        }
        try:
            r = await self._http.post("/v1/chat/completions", json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"LLM 网关调用失败：{e}") from e
        data = r.json()
        usage = data.get("usage") or {}
        return ChatResult(
            model=data.get("model", model),
            content=data["choices"][0]["message"]["content"] or "",
            data_mode="real",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
