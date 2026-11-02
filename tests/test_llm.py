import httpx
import pytest

from fos.config import LLMMode
from fos.gateway.llm import ChatMessage, LLMClient, LLMError


async def test_litellm_mode_sends_tenant_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v3",
                "choices": [{"message": {"role": "assistant", "content": "27 车"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )

    c = LLMClient(LLMMode.litellm, "http://litellm", "sk-test")
    assert c._http is not None
    c._http._transport = httpx.MockTransport(handler)
    res = await c.chat([ChatMessage(role="user", content="烧了几车")], tenant_id="luneng")
    await c.aclose()
    assert res.content == "27 车" and res.data_mode == "real" and res.completion_tokens == 3
    assert seen["body"]["user"] == "luneng" and seen["body"]["model"] == "fos-chat"
    assert seen["auth"] == "Bearer sk-test"


async def test_gateway_error_wrapped():
    c = LLMClient(LLMMode.litellm, "http://litellm")
    assert c._http is not None
    c._http._transport = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(LLMError):
        await c.chat([ChatMessage(role="user", content="x")], tenant_id="t")
    await c.aclose()
