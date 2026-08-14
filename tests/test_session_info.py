"""session_info：会话名/工作区名解析的单元测试。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors
"""

import json
import sys
from pathlib import Path

VOICE_BRIDGE = Path(__file__).resolve().parent.parent / "voice-bridge"
sys.path.insert(0, str(VOICE_BRIDGE))

from session_info import lookup_session_info  # noqa: E402

SID = "session_abc123"


def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / ".kimi-code"
    sdir = home / "sessions" / "wd_demo_0123456789ab" / SID
    sdir.mkdir(parents=True)
    (sdir / "state.json").write_text(
        json.dumps({"id": SID, "title": "我的会话"}), encoding="utf-8"
    )
    (home / "session_index.jsonl").write_text(
        json.dumps({"sessionId": SID, "sessionDir": str(sdir).replace("\\", "/")}) + "\n",
        encoding="utf-8",
    )
    (home / "workspaces.json").write_text(
        json.dumps({"workspaces": {"wd_demo_0123456789ab": {"name": "演示工作区"}}}),
        encoding="utf-8",
    )
    return home


def test_lookup_full(tmp_path):
    info = lookup_session_info(SID, kimi_home=_make_home(tmp_path))
    assert info == {"title": "我的会话", "workspace": "演示工作区"}


def test_lookup_missing_session(tmp_path):
    info = lookup_session_info("session_nope", kimi_home=_make_home(tmp_path))
    assert info == {"title": None, "workspace": None}


def test_lookup_no_home(tmp_path):
    info = lookup_session_info(SID, kimi_home=tmp_path / "nonexistent")
    assert info == {"title": None, "workspace": None}


def test_lookup_none_id(tmp_path):
    assert lookup_session_info(None, kimi_home=tmp_path) == {"title": None, "workspace": None}


def test_lookup_missing_workspace_entry(tmp_path):
    home = _make_home(tmp_path)
    (home / "workspaces.json").write_text('{"workspaces": {}}', encoding="utf-8")
    info = lookup_session_info(SID, kimi_home=home)
    assert info["title"] == "我的会话"
    assert info["workspace"] is None
