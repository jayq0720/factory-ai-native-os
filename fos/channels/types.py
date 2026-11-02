"""统一信道格式（方案第 6 章）。

三种 IM 的消息都先转成 InboundMessage，再进 Trigger 入口；Router / Worker 只看到这一种格式，
回复也只用 ConversationRef + 文本或卡片，不关心是哪个平台。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

ChannelName = Literal["dingtalk", "feishu", "wecom", "mock"]
ConversationType = Literal["direct", "group"]
MessageKind = Literal["text", "image", "file", "audio", "rich", "card_action", "other"]


class Attachment(BaseModel):
    kind: Literal["image", "file", "audio", "video"]
    ref: str = Field(min_length=1, max_length=512)  # 平台下载码 / 文件 key，由适配器取回内容
    filename: str | None = Field(default=None, max_length=255)


class CardAction(BaseModel):
    card_id: str = Field(min_length=1, max_length=64)  # 卡片 id（outTrackId）
    action: str = Field(min_length=1, max_length=32)  # 按钮 id，如 confirm / cancel


class ConversationRef(BaseModel):
    """回复目标。群聊用 conversation_id；单聊还需要对方在平台上的用户 id。"""

    channel: ChannelName
    conversation_id: str = Field(min_length=1, max_length=128)
    conversation_type: ConversationType
    user_id: str | None = Field(default=None, max_length=128)


class InboundMessage(BaseModel):
    channel: ChannelName
    tenant_id: str
    external_msg_id: str = Field(min_length=1, max_length=128)  # 平台消息 id，用于去重
    conversation_id: str = Field(min_length=1, max_length=128)
    conversation_type: ConversationType
    sender_id: str = Field(min_length=1, max_length=128)  # 平台用户 id（钉钉为企业内 userid）
    sender_name: str = Field(default="", max_length=64)
    mentioned: bool = False  # 群聊里是否 @ 了机器人；单聊恒为 True
    kind: MessageKind
    text: str = Field(default="", max_length=4000)
    attachments: list[Attachment] = Field(default_factory=list)
    action: CardAction | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def reply_to(self) -> ConversationRef:
        return ConversationRef(
            channel=self.channel,
            conversation_id=self.conversation_id,
            conversation_type=self.conversation_type,
            user_id=self.sender_id if self.conversation_type == "direct" else None,
        )
