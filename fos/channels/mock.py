"""模拟信道：开发、演示和测试用。发出去的消息记在内存里，附件内容由调用方预先放入。"""

from __future__ import annotations

from typing import Any

from fos.channels.base import ChannelError
from fos.channels.cards import Card, to_markdown
from fos.channels.types import Attachment, ConversationRef


class MockChannel:
    name = "mock"
    supports_card_callback = True

    def __init__(self) -> None:
        self.outbox: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}

    async def send_text(self, to: ConversationRef, text: str) -> None:
        self.outbox.append({"to": to.model_dump(), "type": "text", "text": text})

    async def send_card(self, to: ConversationRef, card: Card, *, card_id: str, short_code: str) -> None:
        self.outbox.append(
            {
                "to": to.model_dump(),
                "type": "card",
                "card_id": card_id,
                "short_code": short_code,
                "card": card.model_dump(),
                "markdown": to_markdown(card, short_code=short_code),
            }
        )

    async def fetch_attachment(self, att: Attachment, *, max_bytes: int) -> bytes:
        data = self.files.get(att.ref)
        if data is None:
            raise ChannelError(f"附件 {att.ref} 不存在")
        if len(data) > max_bytes:
            raise ChannelError("附件过大")
        return data

    async def aclose(self) -> None:
        return None
