"""voice-bridge 适配器：voice-core → kimi web 会话的传声筒。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

对 voice-core 暴露 OpenAI 兼容的 POST /v1/chat/completions（含 SSE 流式），
把语音转出的文字转发给 kimi web 里当前选中的会话。提交即返回（不等目标
会话跑完）。适配器本身不做对话，只做转发、轮询和状态管理。

投递目标抽象在 voice-bridge/targets/（当前固定使用 kimi_web target，
目标选择 router 后续步骤落地）。目标会话 = 浏览器里当前选中的会话
（target_resolver 后台轮询地址栏缓存，发送前再同步刷新一次约 25ms，
刚切换会话就说话也命中正确的目标）。
三种发送模式（voice-bridge/send.mode，控制面板可运行时切换）：
快速发送走 REST 绕过输入框（无法配图）；对话框发送 / 输入对话框走粘贴，
不抢焦点、不代点输入框，前台不是 Kimi Web 或焦点不在输入框则通知。
新建会话草稿页没有会话 id，快速发送会降级为对话框发送。

运行（仓库根目录）：.venv\\Scripts\\python.exe voice-bridge\\adapter.py
本机路径可写在仓库根 .env（见 .env.example），不设则按脚本位置解析。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from envutil import bridge_dir, load_dotenv
from runtime_state import (
    SEND_COMPOSE,
    SEND_FAST,
    SEND_LABELS,
    SEND_MODES,
    SEND_NOTES,
    TARGET_MODES,
    count_hotwords,
    read_send_mode,
    write_gate_cmd,
    write_send_mode,
    write_target_mode,
)
from targets import TargetRouter
from targets.base import ResolvedTarget
from targets.kimi_web import KimiWebTarget

load_dotenv()
BASE_DIR = bridge_dir()
_cfg_path = BASE_DIR / "config.json"
if not _cfg_path.is_file():
    raise SystemExit(
        f"缺少 {_cfg_path}，请复制 voice-bridge/config.example.json 为 config.json"
    )
CONFIG = json.loads(_cfg_path.read_text(encoding="utf-8"))

KIMI = (os.environ.get("KIMI_WEB_BASE") or CONFIG["kimi_web_base"]).rstrip("/")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("voice-bridge")

# 端到端就绪标记：voice-core 模型加载完成后会发预热文本，
# 收到它才算整条链路可用。voice-launcher 据此判断启动完成。
_ready: bool = False
_last_notice: str = ""

_toaster = None


def _notify(text: str) -> None:
    """桥接级提示弹 Windows 通知（语音只进不出，没有朗读通道，
    这些问题不说出来用户会以为指令没发出去）。通知失败不影响主流程。"""
    global _toaster, _last_notice
    _last_notice = text
    try:
        if _toaster is None:
            from windows_toasts import WindowsToaster

            _toaster = WindowsToaster("MicToAgent")
        from windows_toasts import Toast

        _toaster.show_toast(Toast([text]))
    except Exception as e:
        logger.debug("系统通知失败: %s", e)


# 投递目标由 router 选定（target.mode 覆盖 config 的 target，默认 kimi_web；
# auto 按前台窗口路由，kimi 离线时 fallback 到 dsh）
_target = KimiWebTarget(KIMI, CONFIG, _notify)
_router = TargetRouter(CONFIG, _notify, kimi=_target)
# 跟随当前选中会话的解析器（启动事件里 start）。_resolver / _submit_seq /
# _watchers 是 target 内部对象的别名，面板与盯梢流程走这里。
_resolver = _target.resolver
_submit_seq = _target.submit_seq
_watchers = _target.watchers


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _target.start()
    yield


app = FastAPI(title="voice-bridge", lifespan=_lifespan)


# ---------- kimi web 调用（委托给 targets.kimi_web） ----------


async def _kimi_alive(client: httpx.AsyncClient, base: str) -> bool:
    return await _target.is_alive(client, base)


async def _send_prompt(client: httpx.AsyncClient, base: str, sid: str, text: str) -> str:
    return await _target.submit_fast(
        client, ResolvedTarget("kimi_web", base, sid), text)


# ---------- 核心流程 ----------


def _resolve_target() -> tuple[str | None, str | None]:
    """返回跟随到的 (base, 会话 id)；当前没选中任何会话时 sid 为 None。
    先同步刷新一次地址栏（约几十毫秒，UIA 是阻塞 COM 调用，调用方应用
    asyncio.to_thread 包住），保证刚切换会话就说话也命中。"""
    _resolver.refresh()
    return _resolver.current_target()


def _normalize(text: str) -> str:
    return re.sub(r"[\s，。！？!?,.\u3000]+", "", text).lower()


def _extract_last_user_text(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            return "".join(parts).strip()
    return ""


def _paste(text: str, submit: bool) -> str:
    return _target.paste(text, submit)


def _arm_watcher(base: str, sid: str) -> None:
    key = (base, sid)
    _submit_seq[key] = _submit_seq.get(key, 0) + 1
    if key not in _watchers:
        _watchers.add(key)
        asyncio.create_task(_watch_turn(base, sid))


async def _wait_target(timeout: float = 8) -> tuple[str | None, str | None]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.6)
        await asyncio.to_thread(_resolver.refresh)
        base, sid = _resolver.current_target()
        if sid:
            return base, sid
    return None, None


async def handle_user_text(text: str) -> str:
    """按当前发送模式把用户文字交给 Kimi Web。提交即返回（不等执行完）。"""
    global _ready
    if _normalize(text) == _normalize(CONFIG["warmup_text"]):
        _ready = True
        return CONFIG["warmup_reply"]

    mode = read_send_mode()
    # router 选定投递目标（dsh target 接入前 choose 恒返回 kimi target）
    target = _router.choose()
    if target is None:
        return "目标没有接入。"
    if target is not _target:
        return await _handle_dsh_text(target, mode, text)
    # 目标会话在收到文字时就锁定：若等耗时处理完再解析，会把话发到
    # 用户等待期间切换到的会话（实测事故）
    base, sid = await asyncio.to_thread(_resolve_target)
    on_web = _resolver.on_kimi_web()
    target_base = base or KIMI
    logger.info("发送模式=%s 目标=%s 会话=%s 在网页=%s", mode, target_base, sid, on_web)
    # 只打目标 kimi web，忽略系统代理（Clash 等会把 127.0.0.1 拐走）
    async with httpx.AsyncClient(trust_env=False) as client:
        if not await _kimi_alive(client, target_base):
            # §4.2 fallback：仅 auto 模式下，kimi 离线而 dsh 在线时改投 dsh
            fallback = None
            if _router.effective_mode() == "auto":
                fallback = await _router.fallback_dsh(client)
            if fallback is not None:
                logger.info("kimi 离线，auto 改投 dsh")
                return await _handle_dsh_text(fallback, mode, text, client)
            _notify("Kimi Web 没有运行，语音指令没有发出去")
            return "Kimi Web 没有运行。"

        if mode == SEND_FAST and sid:
            status = await _send_prompt(client, target_base, sid, text)
            if status == "queued":
                logger.info("指令已在 kimi web 排队（目标会话正忙）")
            _arm_watcher(target_base, sid)
            return "已发送。"

        if mode == SEND_FAST and not on_web:
            _notify("没看到你在网页里选中会话，请先点一下想说话的会话")
            return "没有选中会话。"

        submit = mode != SEND_COMPOSE
        result = await asyncio.to_thread(_paste, text, submit)
        if result != "ok":
            from draft_input import PASTE_NOTICES

            _notify(PASTE_NOTICES.get(result, PASTE_NOTICES["unavailable"]))
            return "没有放入输入框。"
        if not submit:
            return "已放入输入框。"
        if not sid:
            base, sid = await _wait_target()
            if not sid:
                _notify("已经贴进输入框，但网页还没建成会话")
                return "已发送。"
            target_base = base or KIMI
        logger.info("目标会话: %s @ %s", sid, target_base)
        _arm_watcher(target_base, sid)
        return "已发送。"


async def _watch_turn(base: str, sid: str) -> None:
    await _target.watch_turn(base, sid)


async def _handle_dsh_text(target, mode: str, text: str, client=None) -> str:
    """DSH 投递流程：resolve 同步锁定（收到文字时调一次），fast 走
    session/prompt（绝不重发），粘贴模式复用 draft_input 原语。
    watcher 仍只挂 kimi，dsh 提交后不盯梢（v1 决策）。"""
    resolved = await asyncio.to_thread(target.resolve)
    if not resolved.address:
        _notify("没找到 DSH 桌面版，语音指令没有发出去")
        return "DSH 没有运行。"
    logger.info("发送模式=%s 目标=dsh 会话=%s", mode, resolved.session_id)
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(trust_env=False)
    try:
        if not await target.is_alive(client, resolved.address):
            _notify("DSH 桌面版没有运行，语音指令没有发出去")
            return "DSH 没有运行。"
        if mode == SEND_FAST:
            if not resolved.session_id:
                return "没有选中会话。"
            status = await target.submit_fast(client, resolved, text)
            return "已发送。" if status == "accepted" else "没有发出去。"
        submit = mode != SEND_COMPOSE
        result = await asyncio.to_thread(target.paste, text, submit)
        if result != "ok":
            from draft_input import PASTE_NOTICES

            _notify(PASTE_NOTICES.get(result, PASTE_NOTICES["unavailable"]))
            return "没有放入输入框。"
        return "已发送。" if submit else "已放入输入框。"
    finally:
        if owns_client:
            await client.aclose()


# ---------- OpenAI 兼容接口 ----------

def _completion_payload(text: str, model: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _sse_stream(text: str, model: str):
    def gen():
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        chunk = {
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        end = {
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(end, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "voice-bridge")
    stream = bool(body.get("stream"))
    messages = body.get("messages", [])
    text = _extract_last_user_text(messages)
    logger.info("收到文字: %s", text[:120])

    if not text:
        reply = ""
    else:
        try:
            reply = await handle_user_text(text)
        except Exception as e:
            logger.exception("处理失败")
            _notify(f"语音桥接出了问题：{type(e).__name__}，详情见 voice-bridge 日志")
            reply = f"桥接异常：{type(e).__name__}"
    if reply:
        logger.info("回复文字: %s", reply[:120])

    if stream:
        return _sse_stream(reply, model)
    return JSONResponse(_completion_payload(reply, model))


def _panel_payload() -> dict:
    gate = "0"
    try:
        raw = (BASE_DIR / "gate.state").read_text(encoding="ascii").strip()
        if raw in ("0", "1", "2"):
            gate = raw
    except OSError:
        pass
    return {
        "send_mode": read_send_mode(),
        "ready": _ready,
        "gate": gate,
        **_target.panel_state(),
        # 投递目标路由（target 键已被「session/draft/away」占用，路由状态用新键）
        "target_mode": _router.effective_mode(),
        "target_name": _router.current_name(),
        "target_options": list(TARGET_MODES),
        "hotword_count": count_hotwords(),
        "last_notice": _last_notice,
        "modes": [
            {
                "id": m,
                "label": SEND_LABELS[m],
                "note": SEND_NOTES[m],
                "badge": "无法配图" if m == SEND_FAST else "",
            }
            for m in SEND_MODES
        ],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    """端到端就绪（voice-core 预热文本已到达）。voice-launcher 启动向导用。"""
    return {"ready": _ready}


@app.get("/v1/panel")
async def panel_get():
    """控制面板状态（发送模式、门控、当前会话）。"""
    return _panel_payload()


@app.post("/v1/panel")
async def panel_post(request: Request):
    """控制面板改发送模式、投递目标或请求静音。下一句语音生效。"""
    body = await request.json()
    if "send_mode" in body:
        try:
            write_send_mode(str(body["send_mode"]))
        except ValueError:
            return JSONResponse({"error": "未知发送模式"}, status_code=400)
    if "target" in body:
        try:
            write_target_mode(str(body["target"]))
        except ValueError:
            return JSONResponse({"error": "未知投递目标"}, status_code=400)
    if "mute" in body:
        write_gate_cmd("0" if body["mute"] else "1")
    return _panel_payload()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=CONFIG["listen_host"],
        port=CONFIG["listen_port"],
        log_level="warning",
    )
