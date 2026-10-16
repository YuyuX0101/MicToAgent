"""mic-gate 双模键松开逻辑：一次松开只落定一次状态（图标不闪中间态）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors
"""

import importlib.util
import sys
from pathlib import Path

import pytest

VOICE_BRIDGE = Path(__file__).resolve().parent.parent / "voice-bridge"
sys.path.insert(0, str(VOICE_BRIDGE))


@pytest.fixture()
def mg(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "mic_gate", VOICE_BRIDGE / "mic-gate.pyw")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    syncs = []
    monkeypatch.setattr(mod, "_sync", lambda: syncs.append(mod._gate_value()))
    monkeypatch.setattr(mod, "_ptt_held", True)   # 松开前都处于按住说话
    return mod, syncs


def test_tap_from_closed_single_sync_open(mg, monkeypatch):
    """关门时点按：直接落定常听开，不得经过中间态 "0"（图标闪红的根因）。"""
    mod, syncs = mg
    monkeypatch.setattr(mod, "_toggle_on", False)
    mod._release_dual("tap")
    assert syncs == ["1"]


def test_tap_while_listening_single_sync_closed(mg, monkeypatch):
    mod, syncs = mg
    monkeypatch.setattr(mod, "_toggle_on", True)
    mod._release_dual("tap")
    assert syncs == ["0"]


def test_hold_while_listening_closes_both(mg, monkeypatch):
    """常听中长按说完：松开一次性连常听一起关掉。"""
    mod, syncs = mg
    monkeypatch.setattr(mod, "_toggle_on", True)
    mod._release_dual("hold")
    assert syncs == ["0"]
    assert mod._toggle_on is False


def test_hold_from_closed_stays_closed(mg, monkeypatch):
    mod, syncs = mg
    monkeypatch.setattr(mod, "_toggle_on", False)
    mod._release_dual("hold")
    assert syncs == ["0"]
