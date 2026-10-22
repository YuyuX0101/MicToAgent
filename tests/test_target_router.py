"""目标选择路由（targets.TargetRouter）：auto 分支、手动模式、target.mode 覆盖。"""

import sys
from pathlib import Path

import pytest

VOICE_BRIDGE = Path(__file__).resolve().parent.parent / "voice-bridge"
sys.path.insert(0, str(VOICE_BRIDGE))

from runtime_state import write_target_mode  # noqa: E402
from targets import TargetRouter  # noqa: E402


class FakeTarget:
    def __init__(self, name):
        self.name = name


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime_state.bridge_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture()
def router(state_dir):
    notes = []
    r = TargetRouter({}, notes.append, kimi=FakeTarget("kimi_web"))
    return r, notes


def test_default_is_kimi_web(router):
    r, _ = router
    assert r.effective_mode() == "kimi_web"
    assert r.choose().name == "kimi_web"


def test_config_target_used_when_no_runtime_file(state_dir):
    r = TargetRouter({"target": "kimi_web"}, lambda t: None, kimi=FakeTarget("kimi_web"))
    assert r.effective_mode() == "kimi_web"
    assert r.choose().name == "kimi_web"


def test_runtime_file_overrides_config(state_dir):
    write_target_mode("auto")
    r = TargetRouter({"target": "kimi_web"}, lambda t: None, kimi=FakeTarget("kimi_web"))
    assert r.effective_mode() == "auto"


def test_manual_kimi_ignores_dsh_foreground(router, monkeypatch):
    r, _ = router
    write_target_mode("kimi_web")
    r._dsh = FakeTarget("dsh")
    monkeypatch.setattr(r, "_dsh_foreground", lambda: True)
    assert r.choose().name == "kimi_web"


def test_manual_dsh_without_dsh_target_notifies(router, monkeypatch):
    """dsh target 构造失败（版本不适配等）时手动 dsh 不放弃到 kimi，弹通知。"""
    import targets.dsh

    r, notes = router
    write_target_mode("dsh")

    def boom(*args, **kwargs):
        raise RuntimeError("接口不匹配")

    monkeypatch.setattr(targets.dsh, "DshTarget", boom)
    assert r.choose() is None
    assert len(notes) == 1
    assert "DSH" in notes[0]


def test_manual_dsh_with_dsh_target(router):
    r, notes = router
    write_target_mode("dsh")
    r._dsh = FakeTarget("dsh")
    assert r.choose().name == "dsh"
    assert notes == []


def test_auto_without_dsh_falls_back_to_kimi(router, monkeypatch):
    r, _ = router
    write_target_mode("auto")
    monkeypatch.setattr(r, "_dsh_foreground", lambda: False)
    assert r.choose().name == "kimi_web"


def test_auto_dsh_foreground_picks_dsh(router, monkeypatch):
    r, _ = router
    write_target_mode("auto")
    r._dsh = FakeTarget("dsh")
    monkeypatch.setattr(r, "_dsh_foreground", lambda: True)
    assert r.choose().name == "dsh"
    assert r.current_name() == "dsh"


def test_auto_dsh_not_foreground_stays_kimi(router, monkeypatch):
    r, _ = router
    write_target_mode("auto")
    r._dsh = FakeTarget("dsh")
    monkeypatch.setattr(r, "_dsh_foreground", lambda: False)
    assert r.choose().name == "kimi_web"


def test_auto_dsh_module_missing_stays_kimi(router, monkeypatch):
    """targets.dsh 构造失败时：auto 模式不炸、恒落 kimi_web，只探测一次。"""
    import targets.dsh

    r, notes = router
    write_target_mode("auto")

    def boom(*args, **kwargs):
        raise RuntimeError("接口不匹配")

    monkeypatch.setattr(targets.dsh, "DshTarget", boom)
    assert r._dsh_target() is None
    assert r._dsh_failed is True  # 只探测一次
    assert r.choose().name == "kimi_web"
    assert notes == []


def test_auto_dsh_real_target_constructs(router, monkeypatch):
    """dsh.py 已落地：auto 下能构造出真实 DshTarget（前台不是 DSH 仍落 kimi）。"""
    r, _ = router
    write_target_mode("auto")
    dsh = r._dsh_target()
    assert dsh is not None and dsh.name == "dsh"
    monkeypatch.setattr(r, "_dsh_foreground", lambda: False)
    assert r.choose().name == "kimi_web"
    dsh.stop()


def test_panel_payload_has_target_route_fields(state_dir, monkeypatch):
    import adapter as ad

    # 不依赖本机 config.json 的 target 值（实机可能已切 auto）
    monkeypatch.setitem(ad.CONFIG, "target", "kimi_web")
    payload = ad._panel_payload()
    # 现有字段一个不能少
    for key in (
        "send_mode", "ready", "gate", "session_id", "target_base",
        "session_title", "workspace", "on_kimi_web", "target",
        "hotword_count", "last_notice", "modes",
    ):
        assert key in payload, key
    # 新增路由字段
    assert payload["target_mode"] == "kimi_web"
    assert payload["target_name"] == "kimi_web"
    assert payload["target_options"] == ["auto", "kimi_web", "dsh"]
