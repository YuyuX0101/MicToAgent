"""DSH 当前会话解析：标题剥后缀、session/list 匹配（唯一/重名/零命中/pinned）。"""

import sys
import time
from pathlib import Path

import pytest

VOICE_BRIDGE = Path(__file__).resolve().parent.parent / "voice-bridge"
sys.path.insert(0, str(VOICE_BRIDGE))

import targets.dsh as d  # noqa: E402

SUFFIX = " — DeepSeek Harness"


def _item(sid, title):
    return {"sessionId": sid, "projections": {"values": {"title": title}}}


class TestStripSuffix:
    def test_with_suffix(self):
        assert d.strip_title_suffix("安装插件" + SUFFIX, SUFFIX) == "安装插件"

    def test_without_suffix(self):
        assert d.strip_title_suffix("别的窗口", SUFFIX) is None

    def test_custom_suffix(self):
        assert d.strip_title_suffix("标题 :: DSH", " :: DSH") == "标题"

    def test_empty_title(self):
        assert d.strip_title_suffix("", SUFFIX) is None


def test_session_item_title_reads_projections():
    assert d.session_item_title(_item("s1", "标题")) == "标题"
    assert d.session_item_title({"sessionId": "s2"}) == ""


class TestMatchSession:
    SESSIONS = [_item("s1", "甲"), _item("s2", "乙")]

    def test_unique_hit(self):
        sid, err = d.match_session("甲", self.SESSIONS, None)
        assert (sid, err) == ("s1", None)

    def test_duplicate_titles_no_guess(self):
        sessions = [_item("s1", "同名"), _item("s2", "同名")]
        sid, err = d.match_session("同名", sessions, None)
        assert sid is None
        assert "pinned_session_id" in err

    def test_zero_hit_falls_back_to_pinned(self):
        sid, err = d.match_session("不存在", self.SESSIONS, "s9")
        assert (sid, err) == ("s9", None)

    def test_zero_hit_without_pinned_notifies(self):
        sid, err = d.match_session("不存在", self.SESSIONS, None)
        assert sid is None
        assert "点一下会话" in err

    def test_no_title_uses_pinned(self):
        sid, err = d.match_session(None, self.SESSIONS, "s9")
        assert (sid, err) == ("s9", None)

    def test_empty_session_title_no_false_hit(self):
        # 新会话标题是空串，不能被空标题误命中
        sessions = [_item("s3", "")]
        sid, err = d.match_session("某标题", sessions, None)
        assert sid is None


@pytest.fixture()
def target():
    notes = []
    t = d.DshTarget(
        {"port": 12192, "process_path_hints": ["deepseekharness"]}, notes.append)
    t._sessions = [_item("s1", "甲"), _item("s2", "乙")]
    t._sessions_ts = time.monotonic()
    return t, notes


def _fake_foreground(monkeypatch, title, image_path):
    monkeypatch.setattr(d, "_foreground_window", lambda: (100, title))
    monkeypatch.setattr(d, "_window_process_path", lambda hwnd: image_path)


def test_resolve_foreground_session(target, monkeypatch):
    t, notes = target
    _fake_foreground(monkeypatch, "乙" + SUFFIX, "D:\\Apps\\DeepSeekHarness\\dsh.exe")
    rt = t.resolve()
    assert rt.target_name == "dsh"
    assert rt.address == "127.0.0.1:12192"
    assert rt.session_id == "s2"
    assert rt.session_title == "乙"
    assert notes == []


def test_resolve_not_foreground_uses_pinned(target, monkeypatch):
    t, notes = target
    t._pinned = "s9"
    _fake_foreground(monkeypatch, "别的窗口", "C:\\other\\app.exe")
    rt = t.resolve()
    assert rt.session_id == "s9"
    assert notes == []


def test_resolve_not_foreground_without_pinned_notifies(target, monkeypatch):
    t, notes = target
    _fake_foreground(monkeypatch, "别的窗口", "C:\\other\\app.exe")
    rt = t.resolve()
    assert rt.session_id is None
    assert notes and "点一下会话" in notes[0]


def test_resolve_port_not_found(target, monkeypatch):
    t, _ = target
    t._config = {}  # 不钉死端口
    monkeypatch.setattr(d, "discover_port", lambda hints: None)
    rt = t.resolve()
    assert rt.address is None
    assert rt.session_id is None
