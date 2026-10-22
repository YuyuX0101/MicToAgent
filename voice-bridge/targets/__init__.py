"""投递目标注册表与目标选择路由（router）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

router 决定每句语音投给哪个 target：
- 运行时文件 target.mode（面板切换即时生效）覆盖 config.json 的 target；
  两者都未设置时默认 kimi_web（行为与接入 DSH 前完全一致）。
- 手动模式（kimi_web / dsh）：不看前台，直接用指定 target。
- auto：前台窗口是 DSH → dsh；否则走 kimi 现有逻辑（含 Tailscale 远端
  动态路由，不碰）；fallback 顺序 kimi_web → dsh（kimi 离线且 dsh 在线
  时改投 dsh，见 fallback_dsh）。
dsh target 在 targets/dsh.py；本包对它的引用惰性（函数内 import +
try/except）：dsh.py 缺失或探测失败时 auto/手动都安全落回 kimi_web。
"""

from __future__ import annotations

import asyncio
import logging

from runtime_state import (
    TARGET_DSH,
    TARGET_KIMI_WEB,
    TARGET_MODES,
    read_target_mode,
)
from targets.base import ResolvedTarget, TargetAdapter
from targets.kimi_web import KimiWebTarget

logger = logging.getLogger("voice-bridge.router")

TARGETS = {
    "kimi_web": KimiWebTarget,
}

__all__ = [
    "ResolvedTarget",
    "TargetAdapter",
    "KimiWebTarget",
    "TARGETS",
    "TARGET_MODES",
    "TargetRouter",
]


class TargetRouter:
    """按 target.mode / config 选定投递目标。dsh 缺席时一切落回 kimi_web。"""

    def __init__(self, config: dict, notify, kimi: TargetAdapter) -> None:
        self._config = config
        self._notify = notify
        self._kimi = kimi
        self._dsh: TargetAdapter | None = None
        self._dsh_failed = False

    @property
    def kimi(self) -> TargetAdapter:
        return self._kimi

    def effective_mode(self) -> str:
        """当前生效的选择模式：target.mode 覆盖 config，都未设置默认 kimi_web。"""
        return read_target_mode() or self._config.get("target") or TARGET_KIMI_WEB

    def _dsh_target(self) -> TargetAdapter | None:
        """惰性实例化 dsh target；模块缺失或构造失败只记一次，不再重试。"""
        if self._dsh is None and not self._dsh_failed:
            try:
                from targets.dsh import DshTarget

                self._dsh = DshTarget(
                    self._config.get("dsh") or {},
                    self._notify,
                    poll_interval_sec=self._config.get("follow_poll_interval_sec", 0.7),
                )
                self._dsh.start()
            except Exception as e:
                logger.debug("dsh target 不可用: %s", e)
                self._dsh_failed = True
        return self._dsh

    async def fallback_dsh(self, client) -> TargetAdapter | None:
        """§4.2 fallback：auto 模式下 kimi 离线时，dsh 在线则改投 dsh。"""
        dsh = self._dsh_target()
        if dsh is None:
            return None
        port = await asyncio.to_thread(dsh._ensure_port)
        if not port:
            return None
        if await dsh.is_alive(client, f"127.0.0.1:{port}"):
            return dsh
        return None

    def _dsh_foreground(self) -> bool:
        """前台窗口是不是 DSH 桌面版。dsh 模块缺失时恒 False（走 kimi 逻辑）。"""
        try:
            from targets.dsh import dsh_foreground
        except Exception:
            return False
        try:
            return bool(dsh_foreground(self._config.get("dsh") or {}))
        except Exception as e:
            logger.debug("dsh 前台判定失败: %s", e)
            return False

    def current_name(self) -> str:
        """面板展示用：当前会选中的 target 名（不弹通知、不 resolve）。"""
        mode = self.effective_mode()
        if mode == TARGET_KIMI_WEB:
            return TARGET_KIMI_WEB
        if mode == TARGET_DSH:
            return TARGET_DSH if self._dsh_target() is not None else TARGET_KIMI_WEB
        # auto：前台 DSH → dsh；否则 kimi 现有逻辑（fallback 顺序 kimi_web → dsh，
        # kimi 始终优先；dsh 缺席时恒为 kimi_web）
        if self._dsh_target() is not None and self._dsh_foreground():
            return TARGET_DSH
        return TARGET_KIMI_WEB

    def choose(self) -> TargetAdapter | None:
        """收到文字时同步选定 target（不 resolve；锁定仍由 target.resolve 完成）。
        返回 None = 指定目标不可用（已弹通知），调用方放弃本句。"""
        if self.effective_mode() == TARGET_DSH and self._dsh_target() is None:
            self._notify("DSH 还没有接入，语音指令没有发出去")
            return None
        return self._dsh if self.current_name() == TARGET_DSH else self._kimi
