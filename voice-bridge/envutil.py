"""可选的仓库根 .env 与项目路径解析。

无额外依赖。进程里已有的环境变量优先于 .env。
未设 MICTOAGENT_ROOT 时，项目根 = voice-bridge/ 的上一级（本仓库目录）。
"""

from __future__ import annotations

import os
from pathlib import Path

_BRIDGE = Path(__file__).resolve().parent
_DEFAULT_ROOT = _BRIDGE.parent
_loaded = False


def load_dotenv() -> None:
    """把仓库根目录 .env 读进 os.environ（不覆盖已有变量）。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = _DEFAULT_ROOT / ".env"
    if path.is_file():
        apply_dotenv(path)


def apply_dotenv(path: Path) -> None:
    """解析一份 .env 写入 os.environ（已存在的 key 不动）。供测试直接调用。"""
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = os.path.expandvars(os.path.expanduser(val))


def project_root() -> Path:
    load_dotenv()
    root = os.environ.get("MICTOAGENT_ROOT")
    return Path(root).expanduser().resolve() if root else _DEFAULT_ROOT


def bridge_dir() -> Path:
    return project_root() / "voice-bridge"


def models_dir() -> Path:
    load_dotenv()
    override = os.environ.get("MICTOAGENT_MODELS")
    if override:
        return Path(override).expanduser().resolve()
    return project_root() / "models"
