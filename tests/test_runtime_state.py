"""发送模式与运行时状态文件。"""

import sys
from pathlib import Path

import pytest

VOICE_BRIDGE = Path(__file__).resolve().parent.parent / "voice-bridge"
sys.path.insert(0, str(VOICE_BRIDGE))

from runtime_state import (  # noqa: E402
    CLOSE_QUIT,
    CLOSE_TRAY,
    SEND_DEFAULT,
    autostart_enabled,
    clear_stop_cmd,
    consume_gate_cmd,
    consume_panel_show,
    count_hotwords,
    read_panel_prefs,
    read_send_mode,
    request_full_stop,
    request_panel_show,
    set_autostart,
    stop_requested,
    write_gate_cmd,
    write_panel_prefs,
    write_send_mode,
)


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime_state.bridge_dir", lambda: tmp_path)
    return tmp_path


def test_read_default_when_missing(state_dir):
    assert read_send_mode() == SEND_DEFAULT


def test_write_and_read_roundtrip(state_dir):
    write_send_mode("dialog")
    assert read_send_mode() == "dialog"
    write_send_mode("compose")
    assert read_send_mode() == "compose"


def test_invalid_file_falls_back(state_dir):
    (state_dir / "send.mode").write_text("telepathy", encoding="ascii")
    assert read_send_mode() == SEND_DEFAULT


def test_write_rejects_unknown(state_dir):
    with pytest.raises(ValueError):
        write_send_mode("telepathy")


def test_gate_cmd_consume_once(state_dir):
    write_gate_cmd("0")
    assert consume_gate_cmd() == "0"
    assert consume_gate_cmd() is None


def test_panel_show_consume(state_dir):
    request_panel_show()
    assert consume_panel_show() is True
    assert consume_panel_show() is False


def test_stop_flag_stays_until_cleared(state_dir):
    assert stop_requested() is False
    request_full_stop()
    assert stop_requested() is True
    assert stop_requested() is True
    clear_stop_cmd()
    assert stop_requested() is False


def test_stale_stop_ignored_after_boot(state_dir):
    import time as time_mod

    request_full_stop()
    time_mod.sleep(0.05)
    future_boot = time_mod.time() + 5
    assert stop_requested(since=future_boot) is False
    assert stop_requested() is True


def test_panel_script_compiles():
    src = (VOICE_BRIDGE / "panel.pyw").read_text(encoding="utf-8")
    compile(src, "panel.pyw", "exec")
    assert "create_window" in src
    assert "frameless" in src
    assert "gather_state" in src
    assert "NAV_ITEMS" not in src
    assert "toggle_max" not in src


def test_panel_ui_dist_built():
    """面板前端构建产物必须入库（运行时不依赖 Node）。"""
    dist = VOICE_BRIDGE / "panel-ui" / "dist"
    assert (dist / "index.html").exists()
    assert (dist / "launcher.html").exists()  # 启动向导共用同一套产物
    js = sorted((dist / "assets").glob("*.js"))
    assert js and max(f.stat().st_size for f in js) > 10_000
    # 多页构建会代码分包，模式串可能在任意一个 chunk 里
    built = "".join(f.read_text(encoding="utf-8", errors="ignore") for f in js)
    for mode in ("fast", "dialog", "compose"):
        assert mode in built


def _load_panel_module(monkeypatch, tmp_path):
    """以临时状态目录加载 panel.pyw（只取逻辑，不创建窗口）。"""
    import importlib.util

    monkeypatch.setattr("runtime_state.bridge_dir", lambda: tmp_path)
    spec = importlib.util.spec_from_file_location(
        "mict_panel", VOICE_BRIDGE / "panel.pyw"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 指到没人监听的端口，强制适配器回落路径；状态文件也隔到临时目录
    monkeypatch.setattr(mod, "PANEL_API", "http://127.0.0.1:59998/v1/panel")
    monkeypatch.setattr(mod, "BASE", tmp_path)
    return mod


def test_panel_gather_state_falls_back_to_files(monkeypatch, tmp_path):
    mod = _load_panel_module(monkeypatch, tmp_path)
    (tmp_path / "gate.state").write_text("1", encoding="ascii")
    write_send_mode("dialog")
    st = mod.gather_state()
    assert st["gate"] == "1"
    assert st["send_mode"] == "dialog"
    assert st["ready"] is False
    assert st["prefs"]["close_action"] == CLOSE_TRAY


def test_panel_api_actions_write_state_files(monkeypatch, tmp_path):
    mod = _load_panel_module(monkeypatch, tmp_path)
    api = mod.PanelApi()
    api.set_send_mode("compose")
    assert read_send_mode() == "compose"
    api.set_gate(True)
    assert consume_gate_cmd() == "0"
    api.set_close_action(CLOSE_QUIT)
    assert read_panel_prefs()["close_action"] == CLOSE_QUIT
    api.set_silent_start(True)
    assert read_panel_prefs()["silent_start"] is True
    api.set_show_launcher(False)
    assert read_panel_prefs()["show_launcher"] is False


def test_count_hotwords_skips_comments(tmp_path):
    p = tmp_path / "hotwords.txt"
    p.write_text("# 注释\n\n会话\n错误=>正确\n", encoding="utf-8")
    assert count_hotwords(p) == 2


def test_panel_prefs_default(state_dir):
    assert read_panel_prefs() == {
        "close_action": CLOSE_TRAY,
        "silent_start": False,
        "show_launcher": True,
    }


def test_panel_prefs_roundtrip(state_dir):
    write_panel_prefs({"close_action": CLOSE_QUIT})
    assert read_panel_prefs()["close_action"] == CLOSE_QUIT
    assert read_panel_prefs()["silent_start"] is False


def test_panel_prefs_silent_start_roundtrip(state_dir):
    write_panel_prefs({"silent_start": True})
    assert read_panel_prefs()["silent_start"] is True
    assert read_panel_prefs()["close_action"] == CLOSE_TRAY
    write_panel_prefs({"close_action": CLOSE_QUIT})
    assert read_panel_prefs()["silent_start"] is True
    assert read_panel_prefs()["close_action"] == CLOSE_QUIT


def test_panel_prefs_show_launcher_roundtrip(state_dir):
    write_panel_prefs({"show_launcher": False})
    assert read_panel_prefs()["show_launcher"] is False
    assert read_panel_prefs()["silent_start"] is False  # 两个静默开关互不影响
    write_panel_prefs({"silent_start": True})
    assert read_panel_prefs()["show_launcher"] is False


def test_panel_prefs_invalid_falls_back(state_dir):
    (state_dir / "panel.prefs.json").write_text('{"close_action":"explode"}', encoding="utf-8")
    assert read_panel_prefs()["close_action"] == CLOSE_TRAY


def test_panel_prefs_rejects_unknown(state_dir):
    with pytest.raises(ValueError):
        write_panel_prefs({"close_action": "explode"})


def test_autostart_shortcut_roundtrip(tmp_path, monkeypatch):
    lnk = tmp_path / "MicToAgent.lnk"
    monkeypatch.setattr("runtime_state.startup_shortcut_path", lambda: lnk)
    # CI 上没有 .venv：造一个假的 pythonw.exe 满足 set_autostart 的前置检查
    fake_root = tmp_path / "root"
    pythonw = fake_root / ".venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.touch()
    monkeypatch.setattr("runtime_state.project_root", lambda: fake_root)
    assert autostart_enabled() is False
    set_autostart(True)
    assert lnk.is_file()
    assert autostart_enabled() is True
    set_autostart(False)
    assert not lnk.is_file()
    assert autostart_enabled() is False
