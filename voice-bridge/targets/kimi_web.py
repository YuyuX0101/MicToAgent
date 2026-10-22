"""Kimi Web 投递目标：现有逻辑的封装（自 adapter.py 迁入，行为不变）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

FollowUiResolver 后台轮询浏览器地址栏缓存当前选中会话，发送前同步刷新；
fast 模式 REST 直投（按地址栏 authority 动态路由本机/远端，Tailscale
远端标签页投远端），粘贴模式走 draft_input；盯梢按 (base, 会话) 去重，
需要人工拍板时弹一次 Windows 通知。
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from draft_input import paste_into_composer
from session_info import lookup_session_info
from target_resolver import (
    FollowUiResolver,
    foreground_kimi_state,
    set_address_aliases,
)
from targets.base import ResolvedTarget

logger = logging.getLogger("voice-bridge.kimi_web")


class KimiWebTarget:
    name = "kimi_web"

    def __init__(self, base: str, config: dict, notify) -> None:
        self._config = config
        self._notify = notify
        # 本机私有地址别名（config.json 的 kimi_web_aliases，gitignore 不外传）。
        # KIMI 基址本身自动注册为别名：base 配成 Tailscale/局域网地址时，地址栏
        # 显示的就是这个 authority，无需再手动配 alias 才能识别会话页。
        set_address_aliases([base] + config.get("kimi_web_aliases", []))
        # 跟随当前选中会话的解析器（start 由适配器启动事件调用）
        self.resolver = FollowUiResolver(
            base, config.get("follow_poll_interval_sec", 0.7))
        # 盯梢去重：每个目标（base+会话）同一时刻最多一个后台盯梢任务，连续说
        # 多句话不会起一堆重复任务、也不会重复弹同一条拍板通知。_submit_seq 记录
        # 每个目标累计提交了几条语音指令，盯梢任务据此判断队列里还有没有新指令
        # 要盯。键是 (base, sid)：同一会话 id 可能分别出现在本机和远端 kimi web。
        self._submit_seq: dict = {}
        self._watchers: set = set()

    @property
    def submit_seq(self) -> dict:
        return self._submit_seq

    @property
    def watchers(self) -> set:
        return self._watchers

    def start(self) -> None:
        self.resolver.start()

    async def is_alive(self, client: httpx.AsyncClient, address: str) -> bool:
        try:
            r = await client.get(f"{address}/api/v1/healthz", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def is_foreground(self) -> bool:
        state, _ = foreground_kimi_state(self.resolver.port)
        return state != "away"

    def resolve(self) -> ResolvedTarget:
        """同步刷新一次地址栏（约几十毫秒），保证刚切换会话就说话也命中。"""
        self.resolver.refresh()
        base, sid = self.resolver.current_target()
        return ResolvedTarget(target_name=self.name, address=base, session_id=sid)

    async def submit_fast(
        self, client: httpx.AsyncClient, target: ResolvedTarget, text: str
    ) -> str:
        r = await client.post(
            f"{target.address}/api/v1/sessions/{target.session_id}/prompts",
            json={"content": [{"type": "text", "text": text}]},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
        data = body.get("data", body)
        return data.get("status", "running")

    def paste(self, text: str, submit: bool) -> str:
        return paste_into_composer(text, self.resolver.port, submit=submit)

    def panel_state(self) -> dict:
        base, sid = self.resolver.current_target()
        on_web = self.resolver.on_kimi_web()
        if sid:
            target = "session"
        elif on_web:
            target = "draft"
        else:
            target = "away"
        info = lookup_session_info(sid)
        return {
            "session_id": sid,
            "target_base": base,
            "session_title": info["title"],
            "workspace": info["workspace"],
            "on_kimi_web": on_web,
            "target": target,
        }

    async def watch_turn(self, base: str, sid: str) -> None:
        """后台盯梢一个会话（每目标同一时刻只有一个，见 _watchers 去重）：
        需要人工拍板时弹一次 Windows 通知，其余静默。队列里还有新语音指令
        （_submit_seq 增长）就继续盯，回合跑完且队列已空就退出。"""
        key = (base, sid)
        interval = self._config["poll_interval_sec"]
        deadline = time.monotonic() + self._config["max_wait_sec"]
        start_deadline = time.monotonic() + 120  # 等本轮真正开始（防 busy=false 误判）
        seen_seq = self._submit_seq.get(key, 0)
        seen_busy = False
        notified = False
        async with httpx.AsyncClient(trust_env=False) as client:
            try:
                while time.monotonic() < deadline:
                    try:
                        r = await client.get(
                            f"{base}/api/v1/sessions/{sid}", timeout=10)
                        data = r.json().get("data", {})
                    except Exception as e:
                        logger.warning("轮询状态失败: %s", e)
                        await asyncio.sleep(interval)
                        continue

                    if data.get("pending_interaction") in ("approval", "question"):
                        if not notified:
                            notified = True
                            self._notify(
                                "任务执行到一半需要你拍板，请到 Kimi Web 里处理确认")
                    else:
                        notified = False

                    if data.get("busy") or data.get("main_turn_active"):
                        seen_busy = True
                    else:
                        if self._submit_seq.get(key, 0) > seen_seq:
                            # 队列里还有新指令没起来，继续盯
                            seen_seq = self._submit_seq[key]
                        elif seen_busy:
                            return  # 回合跑完、队列已空
                        elif time.monotonic() > start_deadline:
                            return  # 指令始终没起来，放弃
                    await asyncio.sleep(interval)
            except Exception as e:
                logger.warning("盯梢任务异常: %s", e)
            finally:
                self._watchers.discard(key)
