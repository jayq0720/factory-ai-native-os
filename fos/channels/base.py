"""信道适配器接口：每个 IM 平台实现一份（方案第 6 章）。

收消息由各平台自己的长连接 / 回调负责，解析成 InboundMessage 后交给 Trigger 入口；
这里只约定“往外发”和“取附件”两件事。
"""

from __future__ import annotations

from typing import Protocol

from fos.channels.cards import Card
from fos.channels.types import Attachment, ConversationRef


class ChannelError(RuntimeError):
    pass


class ChannelAdapter(Protocol):
    name: str
    supports_card_callback: bool  # False 时卡片退回 Markdown + 文字指令确认

    async def send_text(self, to: ConversationRef, text: str) -> None: ...

    async def send_card(self, to: ConversationRef, card: Card, *, card_id: str, short_code: str) -> None: ...

    async def fetch_attachment(self, att: Attachment, *, max_bytes: int) -> bytes: ...

    async def aclose(self) -> None: ...
