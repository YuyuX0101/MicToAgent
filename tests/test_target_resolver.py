"""target_resolver / adapter 跟随选中会话逻辑的单元测试。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

UIA 读取本身依赖真实桌面环境，这里只测纯函数和目标选择逻辑。
"""

import sys
from pathlib import Path

import pytest

VOICE_BRIDGE = Path(__file__).resolve().parent.parent / "voice-bridge"
sys.path.insert(0, str(VOICE_BRIDGE))

from target_resolver import (  # noqa: E402
    FollowUiResolver,
    classify_omnibox,
    extract_session_id,
    foreground_kimi_state,
)


class TestExtractSessionId:
    PORT = 58627

    def test_localhost_url(self):
        assert (
            extract_session_id("localhost:58627/sessions/session_abc-123", self.PORT)
            == "session_abc-123"
        )

    def test_ip_url_with_scheme_and_query(self):
        assert (
            extract_session_id(
                "http://127.0.0.1:58627/sessions/session_x9y8?foo=bar", self.PORT
            )
            == "session_x9y8"
        )

    def test_trailing_path_keeps_id_segment(self):
        assert (
            extract_session_id(
                "localhost:58627/sessions/session_abc/terminal", self.PORT
            )
            == "session_abc"
        )

    def test_other_site_rejected(self):
        assert extract_session_id("www.baidu.com/sessions/session_x", self.PORT) is None

    def test_other_port_rejected(self):
        assert extract_session_id("localhost:9999/sessions/session_x", self.PORT) is None

    def test_workspace_home_not_a_session(self):
        assert extract_session_id("localhost:58627/", self.PORT) is None

    def test_empty_value(self):
        assert extract_session_id("", self.PORT) is None


class TestResolverPortParsing:
    def test_port_from_base_url(self):
        r = FollowUiResolver("http://127.0.0.1:58627", 0.7)
        assert r._port == 58627

    def test_default_port(self):
        r = FollowUiResolver("http://example.com", 0.7)
        assert r._port == 80

    def test_current_defaults_to_none(self):
        assert FollowUiResolver("http://127.0.0.1:58627").current() is None


class TestAdapterResolveTarget:
    """adapter._resolve_target：发送前同步刷新，原文转发不加修饰。"""

    @pytest.fixture()
    def adapter(self):
        import adapter as ad

        return ad

    def test_followed_session_returned_after_refresh(self, adapter, monkeypatch):
        class FakeResolver:
            refreshed = False

            def refresh(self):
                self.refreshed = True

            def current(self):
                assert self.refreshed, "必须先 refresh 再读 current"
                return "session_followed-1"

        fake = FakeResolver()
        monkeypatch.setattr(adapter, "_resolver", fake)
        assert adapter._resolve_target() == "session_followed-1"
        assert fake.refreshed

    def test_no_selection_returns_none(self, adapter, monkeypatch):
        class FakeResolver:
            def refresh(self):
                pass

            def current(self):
                return None

        monkeypatch.setattr(adapter, "_resolver", FakeResolver())
        assert adapter._resolve_target() is None


class TestClassifyOmnibox:
    """地址栏三态分类：session / home（草稿页）/ away。"""

    PORT = 58627

    def test_session_page(self):
        assert classify_omnibox(
            "localhost:58627/sessions/session_abc-123", self.PORT
        ) == ("session", "session_abc-123")

    def test_draft_home_bare(self):
        assert classify_omnibox("localhost:58627", self.PORT) == ("home", None)

    def test_draft_home_trailing_slash(self):
        assert classify_omnibox("localhost:58627/", self.PORT) == ("home", None)

    def test_kimi_non_session_path_is_home(self):
        assert classify_omnibox("localhost:58627/settings", self.PORT) == ("home", None)

    def test_other_site_is_away(self):
        assert classify_omnibox("www.baidu.com", self.PORT) == ("away", None)

    def test_other_port_is_away(self):
        assert classify_omnibox("localhost:9999", self.PORT) == ("away", None)

    def test_empty_is_away(self):
        assert classify_omnibox("", self.PORT) == ("away", None)


def test_foreground_without_browser_is_away(monkeypatch):
    monkeypatch.setattr("target_resolver._enum_browser_hwnds", lambda: [])
    assert foreground_kimi_state(58627) == ("away", None)


class TestAdapterDraftTyping:
    """快速发送遇到草稿页时降级为粘贴，不再 POST /prompts。"""

    @pytest.fixture()
    def adapter(self):
        import adapter as ad

        return ad

    @staticmethod
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

    def test_draft_page_types_instead_of_post(self, adapter, monkeypatch):
        import asyncio

        sent = []
        pasted = []

        async def fake_alive(client):
            return True

        async def fake_send(client, sid, text):
            sent.append((sid, text))
            return "running"

        def fake_paste(text, submit):
            pasted.append((text, submit))
            return "ok"

        async def fake_wait(timeout=8):
            return "session_new-1"

        async def fake_watch(sid):
            return None

        monkeypatch.setattr(adapter, "read_send_mode", lambda: "fast")
        monkeypatch.setattr(adapter, "_resolver", self._fake_resolver(None, True))
        monkeypatch.setattr(adapter, "_kimi_alive", fake_alive)
        monkeypatch.setattr(adapter, "_send_prompt", fake_send)
        monkeypatch.setattr(adapter, "_paste", fake_paste)
        monkeypatch.setattr(adapter, "_wait_session_id", fake_wait)
        monkeypatch.setattr(adapter, "_watch_turn", fake_watch)
        adapter._submit_seq.clear()
        adapter._watchers.clear()

        reply = asyncio.run(adapter.handle_user_text("帮我看看这个项目"))

        assert reply == "已发送。"
        assert sent == []
        assert pasted == [("帮我看看这个项目", True)]
        assert adapter._submit_seq.get("session_new-1") == 1
        assert "session_new-1" in adapter._watchers

    def test_draft_type_failure_notifies(self, adapter, monkeypatch):
        import asyncio

        notes = []

        async def fake_alive(client):
            return True

        def fake_paste(text, submit):
            return "not_composer"

        monkeypatch.setattr(adapter, "read_send_mode", lambda: "fast")
        monkeypatch.setattr(adapter, "_resolver", self._fake_resolver(None, True))
        monkeypatch.setattr(adapter, "_kimi_alive", fake_alive)
        monkeypatch.setattr(adapter, "_paste", fake_paste)
        monkeypatch.setattr(adapter, "_notify", notes.append)

        reply = asyncio.run(adapter.handle_user_text("你好"))

        assert "没有放入输入框" in reply
        assert len(notes) == 1
        assert "输入框" in notes[0]

    def test_away_still_plain_notify(self, adapter, monkeypatch):
        """快速发送且不在 kimi web 时维持原来的提示，不尝试粘贴。"""
        import asyncio

        notes = []
        pasted = []

        async def fake_alive(client):
            return True

        def fake_paste(text, submit):
            pasted.append(text)
            return "ok"

        monkeypatch.setattr(adapter, "read_send_mode", lambda: "fast")
        monkeypatch.setattr(adapter, "_resolver", self._fake_resolver(None, False))
        monkeypatch.setattr(adapter, "_kimi_alive", fake_alive)
        monkeypatch.setattr(adapter, "_paste", fake_paste)
        monkeypatch.setattr(adapter, "_notify", notes.append)

        reply = asyncio.run(adapter.handle_user_text("你好"))

        assert "没有选中会话" in reply
        assert pasted == []
        assert len(notes) == 1
