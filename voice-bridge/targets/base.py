"""投递目标接口：ResolvedTarget 数据类 + TargetAdapter 协议。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

把「投递目标」抽象成接口，adapter.py 不直接认识具体目标（DSH 集成计划 §1）。
kimi_web 是现有逻辑的封装（行为零变化），dsh 由后续步骤补上；
目标选择 router 下一步才落地。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass
class ResolvedTarget:
    """收到文字时同步锁定的投递目标（锁定后不再重新解析）。"""

    target_name: str  # "kimi_web" / "dsh"
    address: str | None  # kimi: base URL（未在目标网页时为 None）；dsh: "127.0.0.1:<port>"
    session_id: str | None = None  # None = 未选中会话（草稿页/未知）
    session_title: str = ""  # 面板展示用
    can_fast: bool = True  # 该目标当前可否 REST 直投


class TargetAdapter(Protocol):
    """投递目标协议：每个目标一个实现文件，新增 agent 只加一个文件。"""

    name: str

    def start(self) -> None:
        """后台轮询等（kimi 需要，dsh 可空）。"""
        ...

    async def is_alive(self, client: httpx.AsyncClient, address: str) -> bool:
        """目标应用在不在线。"""
        ...

    def is_foreground(self) -> bool:
        """用户当前是否正看着它（auto 路由用）。"""
        ...

    def resolve(self) -> ResolvedTarget:
        """同步、快（<100ms），收到文字时调用（UIA 是阻塞 COM 调用，
        调用方应用 asyncio.to_thread 包住）。"""
        ...

    async def submit_fast(
        self, client: httpx.AsyncClient, target: ResolvedTarget, text: str
    ) -> str:
        """REST 直投。返回服务端状态（如 "running" / "queued"）。"""
        ...

    def paste(self, text: str, submit: bool) -> str:
        """粘贴进输入框，返回 draft_input 的返回码（ok / not_foreground / ...）。"""
        ...

    def panel_state(self) -> dict:
        """该目标的 /v1/panel 附加字段。"""
        ...
