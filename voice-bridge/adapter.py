"""voice-bridge 适配器：speech-to-speech → kimi web「语音总管」会话的传声筒。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 speech-to-kimi-code contributors（本文件为 speech-to-kimi-code fork 原创）

对 speech-to-speech 暴露 OpenAI 兼容的 POST /v1/chat/completions（含 SSE 流式），
把语音转出的文字原样转发给 kimi web 里的总管会话，等它回复后取最后一条文本返回。
适配器本身不含任何 LLM 调用，只做转发、轮询和状态管理。

运行：E:\\workspace\\speech-to-kimi-code\\.venv\\Scripts\\python.exe adapter.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

BASE_DIR = Path(__file__).resolve().parent
# 本机相关的默认值：hub_cwd 默认取项目根目录（voice-bridge/ 的上一级），
# 开源用户克隆即用，无需在 config.json 里填本机绝对路径
_CONFIG_DEFAULTS = {
    "hub_cwd": str(BASE_DIR.parent),
}
CONFIG = {**_CONFIG_DEFAULTS,
          **json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))}
SESSIONS_FILE = BASE_DIR / "sessions.json"

KIMI = CONFIG["kimi_web_base"].rstrip("/")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("voice-bridge")

app = FastAPI(title="voice-bridge")
_hub_lock = asyncio.Lock()

# 录音门控（调试保留）：真正的门控已移到音频入口（speech-to-kimi-entry.py 的
# LocalAudioStreamer 补丁 + mic-gate 写 gate.state），关门时声音根本进不了 VAD。
# 本开关默认常开，只作调试用；关闭时语音文字直接丢弃（返回空回复，TTS 无声）。
_gate_open: bool = CONFIG.get("gate_default_open", True)

# 端到端就绪标记：s2s 全部模型加载完成后会发预热 Hello，
# 收到它才算整条链路可用。voice-launcher 据此判断启动完成。
_ready: bool = False


# ---------- 会话映射持久化 ----------

def _load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_sessions(data: dict) -> None:
    SESSIONS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- kimi web REST 客户端 ----------

_profile_ensured = False


async def _kimi_alive(client: httpx.AsyncClient) -> bool:
    try:
        r = await client.get(f"{KIMI}/api/v1/healthz", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


async def _ensure_hub_profile(client: httpx.AsyncClient, sid: str) -> None:
    """建会话时传的 agent_config 会被服务端忽略，必须 POST /profile 才生效。
    每个进程只对总管会话补一次。"""
    global _profile_ensured
    if _profile_ensured:
        return
    payload = {
        "agent_config": {
            "model": CONFIG["hub_model"],
            "permission_mode": CONFIG["hub_permission_mode"],
            "thinking": CONFIG["hub_thinking"],
        }
    }
    r = await client.post(f"{KIMI}/api/v1/sessions/{sid}/profile", json=payload, timeout=15)
    r.raise_for_status()
    _profile_ensured = True
    logger.info("总管会话配置已生效: model=%s permission=%s thinking=%s",
                CONFIG["hub_model"], CONFIG["hub_permission_mode"], CONFIG["hub_thinking"])


async def _get_hub_session(client: httpx.AsyncClient) -> str:
    """返回总管会话 id；不存在或已失效则新建。"""
    store = _load_sessions()
    sid = store.get("hub_session_id")
    if sid:
        try:
            r = await client.get(f"{KIMI}/api/v1/sessions/{sid}", timeout=10)
            if r.status_code == 200 and r.json().get("code") == 0:
                await _ensure_hub_profile(client, sid)
                return sid
        except Exception:
            pass
        logger.info("hub session %s 已失效，重建", sid)
    payload = {
        "title": CONFIG["hub_title"],
        "metadata": {"cwd": CONFIG["hub_cwd"]},
    }
    r = await client.post(f"{KIMI}/api/v1/sessions", json=payload, timeout=30)
    r.raise_for_status()
    body = r.json()
    sid = body["data"]["id"] if body.get("code") == 0 else body["id"]
    store["hub_session_id"] = sid
    _save_sessions(store)
    logger.info("新建总管会话 %s", sid)
    await _ensure_hub_profile(client, sid)
    return sid


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


async def _wait_turn_done(client: httpx.AsyncClient, sid: str) -> str:
    """两段式轮询：先等本轮真正开始（busy 变 true），再等它结束。
    防止 prompt 提交后 turn 还没 start 时 busy=false 被误判为完成。
    返回 done / need_interaction / timeout。"""
    interval = CONFIG["poll_interval_sec"]

    async def _poll() -> dict:
        r = await client.get(f"{KIMI}/api/v1/sessions/{sid}", timeout=10)
        return r.json().get("data", {})

    # 第一段：等 turn 开始（排队场景给足时间）
    start_deadline = time.monotonic() + 120
    started = False
    while time.monotonic() < start_deadline:
        try:
            data = await _poll()
            if data.get("busy") or data.get("main_turn_active"):
                started = True
                break
            if data.get("pending_interaction") in ("approval", "question"):
                return "need_interaction"
        except Exception as e:
            logger.warning("轮询状态失败: %s", e)
        await asyncio.sleep(1)
    if not started:
        return "timeout"

    # 第二段：等 turn 结束
    deadline = time.monotonic() + CONFIG["max_wait_sec"]
    while time.monotonic() < deadline:
        try:
            data = await _poll()
            pending = data.get("pending_interaction", "none")
            if pending in ("approval", "question"):
                return "need_interaction"
            if not data.get("busy") and not data.get("main_turn_active"):
                return "done"
        except Exception as e:
            logger.warning("轮询状态失败: %s", e)
        await asyncio.sleep(interval)
    return "timeout"


async def _last_assistant_text(client: httpx.AsyncClient, sid: str) -> str:
    r = await client.get(f"{KIMI}/api/v1/sessions/{sid}/messages", timeout=30)
    r.raise_for_status()
    body = r.json()
    # 注意：items 是按时间倒序返回的（最新在最前），不要 reversed
    items = body.get("data", {}).get("items", body.get("items", []))
    for msg in items:
        if msg.get("role") != "assistant":
            continue
        parts = [
            c.get("text", "")
            for c in msg.get("content", [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        text = "".join(parts).strip()
        if text:
            return text
    return "（总管没有回复文字）"


# ---------- 核心流程 ----------

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


async def handle_user_text(text: str) -> str:
    """把用户文字交给总管会话，返回要读出来的回复。"""
    global _ready
    if _normalize(text) == _normalize(CONFIG["warmup_text"]):
        _ready = True
        return CONFIG["warmup_reply"]

    if not _gate_open:
        logger.info("门控关闭，丢弃: %s", text[:60])
        return ""

    async with _hub_lock, httpx.AsyncClient() as client:
        if not await _kimi_alive(client):
            return "Kimi Web 没有运行，请先双击启动它，然后再跟我说话。"

        sid = await _get_hub_session(client)
        status = await _send_prompt(client, sid, text)
        if status == "queued":
            logger.info("指令已排队（总管正忙）")
        result = await _wait_turn_done(client, sid)

        if result == "need_interaction":
            return "我执行到一半需要你拍板，请到 Kimi Web 里看一下那个确认，处理完跟我说一声。"
        if result == "timeout":
            return "这个任务还在执行中，过一会儿可以问我进度怎么样。"

        reply = await _last_assistant_text(client, sid)
        limit = CONFIG["reply_max_chars"]
        if len(reply) > limit:
            reply = reply[:limit] + "……先说到这，细节可以来问我。"
        return reply


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
        reply = "我没有听清，请再说一遍。"
    else:
        try:
            reply = await handle_user_text(text)
        except Exception as e:
            logger.exception("处理失败")
            reply = f"中间桥接出了点问题：{type(e).__name__}，请查看 voice-bridge 日志。"
    logger.info("回复文字: %s", reply[:120])

    if stream:
        return _sse_stream(reply, model)
    return JSONResponse(_completion_payload(reply, model))


@app.get("/health")
async def health():
    return {"status": "ok", "gate_open": _gate_open}


@app.get("/ready")
async def ready():
    """端到端就绪（s2s 预热 Hello 已到达）。voice-launcher 启动向导用。"""
    return {"ready": _ready}


@app.get("/gate")
async def get_gate():
    return {"open": _gate_open}


@app.post("/gate")
async def set_gate(request: Request):
    global _gate_open
    body = await request.json()
    _gate_open = bool(body.get("open"))
    logger.info("门控切换: %s", "开" if _gate_open else "关")
    return {"open": _gate_open}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=CONFIG["listen_host"],
        port=CONFIG["listen_port"],
        log_level="warning",
    )
