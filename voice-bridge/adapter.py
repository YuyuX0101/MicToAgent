"""voice-bridge 适配器：voice-core → kimi web 会话的传声筒。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

对 voice-core 暴露 OpenAI 兼容的 POST /v1/chat/completions（含 SSE 流式），
把语音转出的文字转发给 kimi web 里当前选中的会话。提交即返回（不等目标
会话跑完）。适配器本身不做对话，只做转发、轮询和状态管理。

目标会话 = 浏览器里当前选中的会话（target_resolver 后台轮询地址栏缓存，
发送前再同步刷新一次约 25ms，刚切换会话就说话也命中正确的目标）。
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
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from envutil import bridge_dir, load_dotenv
from session_info import lookup_session_info
from runtime_state import (
    SEND_COMPOSE,
    SEND_FAST,
    SEND_LABELS,
    SEND_MODES,
    SEND_NOTES,
    count_hotwords,
    read_send_mode,
    write_gate_cmd,
    write_send_mode,
)

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

# 跟随当前选中会话的解析器（启动事件里 start）
from target_resolver import FollowUiResolver, set_address_aliases

# 本机私有地址别名（config.json 的 kimi_web_aliases，gitignore 不外传）
set_address_aliases(CONFIG.get("kimi_web_aliases", []))

_resolver = FollowUiResolver(KIMI, CONFIG.get("follow_poll_interval_sec", 0.7))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _resolver.start()
    yield


app = FastAPI(title="voice-bridge", lifespan=_lifespan)


# ---------- kimi web REST 客户端 ----------

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


async def _kimi_alive(client: httpx.AsyncClient) -> bool:
    try:
        r = await client.get(f"{KIMI}/api/v1/healthz", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


async def _send_prompt(client: httpx.AsyncClient, sid: str, text: str) -> str:
    r = await client.post(
        f"{KIMI}/api/v1/sessions/{sid}/prompts",
        json={"content": [{"type": "text", "text": text}]},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    data = body.get("data", body)
    return data.get("status", "running")


# 盯梢去重：每个会话同一时刻最多一个后台盯梢任务，连续说多句话不会
# 起一堆重复任务、也不会重复弹同一条拍板通知。_submit_seq 记录每个会话
# 累计提交了几条语音指令，盯梢任务据此判断队列里还有没有新指令要盯。
_submit_seq: dict[str, int] = {}
_watchers: set[str] = set()


# ---------- 核心流程 ----------


def _resolve_target() -> str | None:
    """返回跟随到的会话 id；当前没选中任何会话时返回 None。
    先同步刷新一次地址栏（约几十毫秒，UIA 是阻塞 COM 调用，调用方应用
    asyncio.to_thread 包住），保证刚切换会话就说话也命中。"""
    _resolver.refresh()
    return _resolver.current()


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
    from draft_input import paste_into_composer

    return paste_into_composer(text, _resolver.port, submit=submit)


def _arm_watcher(sid: str) -> None:
    _submit_seq[sid] = _submit_seq.get(sid, 0) + 1
    if sid not in _watchers:
        _watchers.add(sid)
        asyncio.create_task(_watch_turn(sid))


async def _wait_session_id(timeout: float = 8) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.6)
        await asyncio.to_thread(_resolver.refresh)
        sid = _resolver.current()
        if sid:
            return sid
    return None


async def handle_user_text(text: str) -> str:
    """按当前发送模式把用户文字交给 Kimi Web。提交即返回（不等执行完）。"""
    global _ready
    if _normalize(text) == _normalize(CONFIG["warmup_text"]):
        _ready = True
        return CONFIG["warmup_reply"]

    mode = read_send_mode()
    # 只打本机 kimi web，忽略系统代理（Clash 等会把 127.0.0.1 拐走）
    async with httpx.AsyncClient(trust_env=False) as client:
        if not await _kimi_alive(client):
            _notify("Kimi Web 没有运行，语音指令没有发出去")
            return "Kimi Web 没有运行。"

        # 目标会话在收到文字时就锁定：若等耗时处理完再解析，会把话发到
        # 用户等待期间切换到的会话（实测事故）
        sid = await asyncio.to_thread(_resolve_target)
        on_web = _resolver.on_kimi_web()
        logger.info("发送模式=%s 会话=%s 在网页=%s", mode, sid, on_web)

        if mode == SEND_FAST and sid:
            status = await _send_prompt(client, sid, text)
            if status == "queued":
                logger.info("指令已在 kimi web 排队（目标会话正忙）")
            _arm_watcher(sid)
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
            sid = await _wait_session_id()
            if not sid:
                _notify("已经贴进输入框，但网页还没建成会话")
                return "已发送。"
        logger.info("目标会话: %s", sid)
        _arm_watcher(sid)
        return "已发送。"


async def _watch_turn(sid: str) -> None:
    """后台盯梢一个会话（每会话同一时刻只有一个，见 _watchers 去重）：
    需要人工拍板时弹一次 Windows 通知，其余静默。队列里还有新语音指令
    （_submit_seq 增长）就继续盯，回合跑完且队列已空就退出。"""
    interval = CONFIG["poll_interval_sec"]
    deadline = time.monotonic() + CONFIG["max_wait_sec"]
    start_deadline = time.monotonic() + 120  # 等本轮真正开始（防 busy=false 误判）
    seen_seq = _submit_seq.get(sid, 0)
    seen_busy = False
    notified = False
    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            while time.monotonic() < deadline:
                try:
                    r = await client.get(f"{KIMI}/api/v1/sessions/{sid}", timeout=10)
                    data = r.json().get("data", {})
                except Exception as e:
                    logger.warning("轮询状态失败: %s", e)
                    await asyncio.sleep(interval)
                    continue

                if data.get("pending_interaction") in ("approval", "question"):
                    if not notified:
                        notified = True
                        _notify("任务执行到一半需要你拍板，请到 Kimi Web 里处理确认")
                else:
                    notified = False

                if data.get("busy") or data.get("main_turn_active"):
                    seen_busy = True
                else:
                    if _submit_seq.get(sid, 0) > seen_seq:
                        # 队列里还有新指令没起来，继续盯
                        seen_seq = _submit_seq[sid]
                    elif seen_busy:
                        return  # 回合跑完、队列已空
                    elif time.monotonic() > start_deadline:
                        return  # 指令始终没起来，放弃
                await asyncio.sleep(interval)
        except Exception as e:
            logger.warning("盯梢任务异常: %s", e)
        finally:
            _watchers.discard(sid)


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
    sid = _resolver.current()
    on_web = _resolver.on_kimi_web()
    if sid:
        target = "session"
    elif on_web:
        target = "draft"
    else:
        target = "away"
    info = lookup_session_info(sid)
    return {
        "send_mode": read_send_mode(),
        "ready": _ready,
        "gate": gate,
        "session_id": sid,
        "session_title": info["title"],
        "workspace": info["workspace"],
        "on_kimi_web": on_web,
        "target": target,
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
    """控制面板改发送模式或请求静音。下一句语音生效。"""
    body = await request.json()
    if "send_mode" in body:
        try:
            write_send_mode(str(body["send_mode"]))
        except ValueError:
            return JSONResponse({"error": "未知发送模式"}, status_code=400)
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
