"""粘贴路径：不抢焦点、先图后字、按模式决定是否回车。"""

import sys
from pathlib import Path

VOICE_BRIDGE = Path(__file__).resolve().parent.parent / "voice-bridge"
sys.path.insert(0, str(VOICE_BRIDGE))

import draft_input as d  # noqa: E402


def test_no_force_foreground():
    assert not hasattr(d, "_force_foreground")


def test_omnibox_rejected():
    assert not d.is_composer_focus(
        control_type="EditControl",
        name="Address and search bar",
        top_inset=40,
        control_height=24,
        window_height=800,
    )


def test_toolbar_edit_rejected():
    assert not d.is_composer_focus(
        control_type="EditControl",
        name="Ask",
        top_inset=20,
        control_height=24,
        window_height=800,
    )


def test_page_edit_accepted():
    assert d.is_composer_focus(
        control_type="EditControl",
        name="Ask Kimi",
        top_inset=420,
        control_height=80,
        window_height=800,
    )


def test_full_page_document_rejected():
    assert not d.is_composer_focus(
        control_type="DocumentControl",
        name="",
        top_inset=100,
        control_height=700,
        window_height=800,
    )


def test_composer_document_accepted():
    assert d.is_composer_focus(
        control_type="DocumentControl",
        name="",
        top_inset=500,
        control_height=120,
        window_height=800,
    )


def test_paste_skips_when_not_foreground(monkeypatch):
    monkeypatch.setattr(d, "_HAS_WIN", True)
    monkeypatch.setattr(d, "foreground_kimi_state", lambda port: ("away", None))
    keys = []
    monkeypatch.setattr(d, "_send_keys", lambda *a: keys.append(a))
    assert d.paste_into_composer("你好", 58627, True) == d.NOT_FOREGROUND
    assert keys == []


def test_paste_skips_when_composer_unfocused(monkeypatch):
    monkeypatch.setattr(d, "_HAS_WIN", True)
    monkeypatch.setattr(d, "foreground_kimi_state", lambda port: ("session", "s1"))
    monkeypatch.setattr(d, "_foreground_hwnd", lambda: 1)
    monkeypatch.setattr(d, "_composer_focused", lambda hwnd: False)
    keys = []
    monkeypatch.setattr(d, "_send_keys", lambda *a: keys.append(a))
    assert d.paste_into_composer("你好", 58627, True) == d.NOT_COMPOSER
    assert keys == []


def _stub_ready(monkeypatch):
    monkeypatch.setattr(d, "_HAS_WIN", True)
    monkeypatch.setattr(d, "foreground_kimi_state", lambda port: ("session", "s1"))
    monkeypatch.setattr(d, "_foreground_hwnd", lambda: 1)
    monkeypatch.setattr(d, "_composer_focused", lambda hwnd: True)
    monkeypatch.setattr(d, "_IMAGE_SETTLE_SEC", 0)
    monkeypatch.setattr(d, "_KEY_GAP_SEC", 0)


def test_image_then_text_then_enter(monkeypatch):
    _stub_ready(monkeypatch)
    calls = []
    monkeypatch.setattr(d, "_snapshot_clipboard", lambda: [(d._CF_DIB, 1)])
    monkeypatch.setattr(d, "_snapshot_has_image", lambda snap: True)
    monkeypatch.setattr(d, "_set_clipboard_text", lambda t: calls.append(("set", t)) or True)
    monkeypatch.setattr(d, "_send_keys", lambda *a: calls.append(("keys", a)))
    monkeypatch.setattr(d, "_restore_clipboard", lambda snap: calls.append("restore"))

    assert d.paste_into_composer("你好", 58627, True) == d.OK
    assert calls[0] == ("keys", (d._VK_CONTROL, d._VK_V))
    assert calls[1] == ("set", "你好")
    assert calls[2] == ("keys", (d._VK_CONTROL, d._VK_V))
    assert calls[3] == ("keys", (d._VK_RETURN,))
    assert calls[4] == "restore"


def test_compose_does_not_press_enter(monkeypatch):
    _stub_ready(monkeypatch)
    calls = []
    monkeypatch.setattr(d, "_snapshot_clipboard", lambda: [])
    monkeypatch.setattr(d, "_snapshot_has_image", lambda snap: False)
    monkeypatch.setattr(d, "_set_clipboard_text", lambda t: True)
    monkeypatch.setattr(d, "_send_keys", lambda *a: calls.append(a))
    monkeypatch.setattr(d, "_restore_clipboard", lambda snap: None)

    assert d.paste_into_composer("草稿", 58627, False) == d.OK
    assert (d._VK_RETURN,) not in calls
    assert (d._VK_CONTROL, d._VK_V) in calls
