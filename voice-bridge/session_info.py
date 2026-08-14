"""会话/工作区名称解析：读 kimi CLI 的本地数据文件。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

适配器从地址栏只能拿到会话 id；展示用的会话名和工作区名读本地文件：
- ~/.kimi-code/session_index.jsonl   sessionId -> sessionDir
- <sessionDir>/state.json            title = 会话名（Web 里改名即写回这里）
- <sessionDir>/.. 的目录名            wd_xxx 工作区 id
- ~/.kimi-code/workspaces.json       wd_xxx -> name = 工作区名

只读、失败静默降级为 None；文件都很小，面板轮询频率下直读即可。
"""

from __future__ import annotations

import json
from pathlib import Path


def _kimi_home() -> Path:
    return Path.home() / ".kimi-code"


def _session_dir(session_id: str, home: Path) -> Path | None:
    try:
        lines = (home / "session_index.jsonl").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return None
    for line in lines:
        if session_id not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("sessionId") == session_id and rec.get("sessionDir"):
            return Path(rec["sessionDir"])
    return None


def lookup_session_info(session_id: str | None, kimi_home=None) -> dict:
    """session_id -> {"title": 会话名|None, "workspace": 工作区名|None}。"""
    info = {"title": None, "workspace": None}
    if not session_id:
        return info
    home = Path(kimi_home) if kimi_home else _kimi_home()
    session_dir = _session_dir(session_id, home)
    if session_dir is None:
        return info
    try:
        st = json.loads(
            (session_dir / "state.json").read_text(encoding="utf-8", errors="replace")
        )
        if st.get("title"):
            info["title"] = st["title"]
    except (OSError, json.JSONDecodeError):
        pass
    try:
        ws = json.loads(
            (home / "workspaces.json").read_text(encoding="utf-8", errors="replace")
        )
        name = (ws.get("workspaces") or {}).get(session_dir.parent.name, {}).get("name")
        if name:
            info["workspace"] = name
    except (OSError, json.JSONDecodeError):
        pass
    return info
