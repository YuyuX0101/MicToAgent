"""三种发送模式的适配器路由。"""

import asyncio
import sys
from pathlib import Path

import pytest

VOICE_BRIDGE = Path(__file__).resolve().parent.parent / "voice-bridge"
sys.path.insert(0, str(VOICE_BRIDGE))


@pytest.fixture()
def adapter():
    import adapter as ad

    return ad


def _fake_resolver(sid=None, on_web=True):
    class FakeResolver:
        port = 58627

        def refresh(self):
            pass

        def current(self):
            return sid

        def on_kimi_web(self):
            return on_web

    return FakeResolver()


def _prep(adapter, monkeypatch, *, mode, sid="session_1", on_web=True):
    sent = []
    pasted = []
    notes = []

    async def fake_alive(client):
        return True

    async def fake_send(client, sid_, text):
        sent.append((sid_, text))
        return "running"

    def fake_paste(text, submit):
        pasted.append((text, submit))
        return "ok"

    async def fake_watch(sid_):
        return None

    monkeypatch.setattr(adapter, "read_send_mode", lambda: mode)
    monkeypatch.setattr(adapter, "_resolver", _fake_resolver(sid, on_web))
    monkeypatch.setattr(adapter, "_kimi_alive", fake_alive)
    monkeypatch.setattr(adapter, "_send_prompt", fake_send)
    monkeypatch.setattr(adapter, "_paste", fake_paste)
    monkeypatch.setattr(adapter, "_watch_turn", fake_watch)
    monkeypatch.setattr(adapter, "_notify", notes.append)
    adapter._submit_seq.clear()
    adapter._watchers.clear()
    return sent, pasted, notes


def test_fast_existing_session_uses_rest(adapter, monkeypatch):
    sent, pasted, _ = _prep(adapter, monkeypatch, mode="fast")
    reply = asyncio.run(adapter.handle_user_text("你好"))
    assert reply == "已发送。"
    assert sent == [("session_1", "你好")]
    assert pasted == []
    assert adapter._submit_seq.get("session_1") == 1


def test_dialog_existing_session_pastes_and_enters(adapter, monkeypatch):
    sent, pasted, _ = _prep(adapter, monkeypatch, mode="dialog")
    reply = asyncio.run(adapter.handle_user_text("带图"))
    assert reply == "已发送。"
    assert sent == []
    assert pasted == [("带图", True)]
    assert adapter._submit_seq.get("session_1") == 1


def test_compose_puts_in_box_without_watch(adapter, monkeypatch):
    sent, pasted, _ = _prep(adapter, monkeypatch, mode="compose")
    reply = asyncio.run(adapter.handle_user_text("先别发"))
    assert reply == "已放入输入框。"
    assert sent == []
    assert pasted == [("先别发", False)]
    assert adapter._submit_seq == {}
    assert adapter._watchers == set()


def test_dialog_not_foreground_notifies(adapter, monkeypatch):
    sent, pasted, notes = _prep(adapter, monkeypatch, mode="dialog")

    def fake_paste(text, submit):
        pasted.append((text, submit))
        return "not_foreground"

    monkeypatch.setattr(adapter, "_paste", fake_paste)
    reply = asyncio.run(adapter.handle_user_text("你好"))
    assert "没有放入输入框" in reply
    assert sent == []
    assert notes and "Kimi Web" in notes[0]
    assert adapter._submit_seq == {}
