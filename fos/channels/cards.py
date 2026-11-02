"""卡片 DSL（方案 5.2 按需界面）。

Agent 只描述“标题、正文、字段、按钮”，各适配器渲染成钉钉互动卡片 / 企业微信模板卡片 / 飞书消息卡片。
平台不支持按钮回调时退回 Markdown，并提示用文字指令确认（`确认 K7Q2M`），保证任何平台都能完成 L2 确认。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

MOCK_BANNER = "【模拟数据，非真实识别结果】"


class CardField(BaseModel):
    label: str = Field(min_length=1, max_length=20)
    value: str = Field(max_length=200)


class CardButton(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z_]{0,31}$")
    label: str = Field(min_length=1, max_length=10)
    style: Literal["primary", "danger", "default"] = "default"


class CardLink(BaseModel):
    label: str = Field(min_length=1, max_length=20)
    url: str = Field(pattern=r"^https://", max_length=500)


class Card(BaseModel):
    title: str = Field(min_length=1, max_length=40)
    text: str = Field(default="", max_length=500)  # 简单 Markdown：加粗、换行、列表
    fields: list[CardField] = Field(default_factory=list, max_length=12)
    buttons: list[CardButton] = Field(default_factory=list, max_length=3)
    link: CardLink | None = None
    data_mode: Literal["real", "mock"] = "real"
    footer: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def _unique_buttons(self) -> Card:
        ids = [b.id for b in self.buttons]
        if len(ids) != len(set(ids)):
            raise ValueError("按钮 id 不能重复")
        return self

    def button(self, word: str) -> CardButton | None:
        """按 id 或按钮文字查找按钮（文字指令确认用）。"""
        w = word.strip().lower()
        return next((b for b in self.buttons if w in (b.id, b.label.lower())), None)


def to_markdown(card: Card, *, short_code: str | None = None, mention_hint: bool = False) -> str:
    """通用 Markdown 渲染：用于不支持互动卡片的场景，也作为各平台卡片正文。"""
    lines: list[str] = []
    if card.data_mode == "mock":
        lines.append(f"**{MOCK_BANNER}**")
    lines.append(f"### {card.title}")
    if card.text:
        lines.append(card.text)
    for f in card.fields:
        lines.append(f"- {f.label}：{f.value}")
    if card.link:
        lines.append(f"[{card.link.label}]({card.link.url})")
    if card.buttons and short_code:
        at = "@我 " if mention_hint else ""
        options = "、".join(f"「{at}{b.label} {short_code}」" for b in card.buttons)
        lines.append(f"请回复 {options}")
    if card.footer:
        lines.append(f"> {card.footer}")
    return "\n\n".join(lines)
