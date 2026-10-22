"""DSH RPC：信封结构、args 按参数名编码、业务错误解析、401 处理路径。"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

VOICE_BRIDGE = Path(__file__).resolve().parent.parent / "voice-bridge"
sys.path.insert(0, str(VOICE_BRIDGE))

import targets.dsh as d  # noqa: E402
from targets.base import ResolvedTarget  # noqa: E402

SECRET = bytes(range(32))
PORT = 12192
ADDRESS = f"127.0.0.1:{PORT}"


@pytest.fixture()
def target():
    notes = []
    t = d.DshTarget({"port": PORT}, notes.append)
    t._signer = d._CookieSigner(SECRET)  # 跳过读真实 credentials.yaml
    return t, notes


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)


def _run(coro):
    return asyncio.run(coro)


def test_submit_fast_envelope_and_args(target):
    t, notes = target
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "type": "server-response",
            "rpcId": "x",
            "result": {"ok": True, "value": {"accepted": True}},
        })

    async def go():
        async with _client(handler) as client:
            return await t.submit_fast(
                client, ResolvedTarget("dsh", ADDRESS, "sess_1"), "你好")

    assert _run(go()) == "accepted"
    assert notes == []
    req = seen[0]
    assert req.url.path == "/api/session/prompt"
    assert req.headers["content-type"].startswith("application/json")
    assert "dsh-auth-" in req.headers.get("cookie", "")
    envelope = json.loads(req.content)
    assert envelope["type"] == "client-request"
    assert envelope["rpcId"]
    assert envelope["method"] == "session/prompt"
    args = envelope["payload"]["args"]
    # args 按参数名编码：session/prompt 的参数名是 request
    assert set(args) == {"request"}
    inner = args["request"]
    assert inner["sessionId"] == "sess_1"
    assert inner["mode"] == "queue"
    assert inner["content"] == [{"type": "text", "text": "你好"}]
    assert inner["requestId"]


def test_submit_fast_requestid_fresh_each_time(target):
    t, _ = target
    ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids.append(json.loads(request.content)["payload"]["args"]["request"]["requestId"])
        return httpx.Response(200, json={"result": {"ok": True, "value": {"accepted": True}}})

    async def go():
        async with _client(handler) as client:
            rt = ResolvedTarget("dsh", ADDRESS, "sess_1")
            await t.submit_fast(client, rt, "一")
            await t.submit_fast(client, rt, "二")

    _run(go())
    assert len(set(ids)) == 2  # requestId 每次提交新生成


def test_submit_fast_business_error_notifies(target):
    t, notes = target

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "result": {"ok": False, "error": {"message": "session gone"}},
        })

    async def go():
        async with _client(handler) as client:
            return await t.submit_fast(
                client, ResolvedTarget("dsh", ADDRESS, "sess_1"), "你好")

    assert _run(go()) == "failed"
    assert len(notes) == 1
    assert "session gone" in notes[0]


def test_submit_fast_401_invalidates_port_and_gives_up(target):
    """401 = 作废端口缓存、放弃并通知，绝不重发原文。"""
    t, notes = target
    t._port_cache = (PORT, 1e18)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, text="unauthorized")

    async def go():
        async with _client(handler) as client:
            return await t.submit_fast(
                client, ResolvedTarget("dsh", ADDRESS, "sess_1"), "你好")

    assert _run(go()) == "failed"
    assert len(calls) == 1  # 一次请求一次发送
    assert t._port_cache is None
    assert notes and "认证失败" in notes[0]


def test_submit_fast_rejects_non_loopback_authority(target):
    t, notes = target

    async def go():
        async with _client(lambda r: httpx.Response(200)) as client:
            return await t.submit_fast(
                client, ResolvedTarget("dsh", "192.168.1.2:12192", "sess_1"), "你好")

    assert _run(go()) == "failed"  # 带 Cookie 的请求只发 127.0.0.1
    assert notes


def test_submit_fast_not_accepted_notifies(target):
    t, notes = target

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"ok": True, "value": {"accepted": False}}})

    async def go():
        async with _client(handler) as client:
            return await t.submit_fast(
                client, ResolvedTarget("dsh", ADDRESS, "sess_1"), "你好")

    assert _run(go()) == "failed"
    assert notes
