"""voice-bridge 长段转写整理的单元测试（adapter.py）。

覆盖 stream-json 输出解析（_extract_assistant_text）与清理阈值常量。
整理本身走 kimi CLI 一次性模式，集成行为不在单测内（需要真实模型）。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parent.parent
BRIDGE = PROJ / "voice-bridge"


@pytest.fixture(scope="module")
def adapter():
    sys.path.insert(0, str(BRIDGE))   # adapter 内 `from target_resolver import ...`
    try:
        spec = importlib.util.spec_from_file_location(
            "voice_bridge_adapter", BRIDGE / "adapter.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["voice_bridge_adapter"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(BRIDGE))


def test_extract_last_assistant_text(adapter):
    output = "\n".join([
        '{"role":"meta","type":"system.version","version":"0.35.0"}',
        '{"role":"assistant","content":"整理后的第一段。"}',
        '{"role":"assistant","content":"最终整理结果。"}',
        '{"role":"meta","type":"session.resume_hint","content":"To resume..."}',
    ])
    assert adapter._extract_assistant_text(output) == "最终整理结果。"


def test_extract_skips_garbage_lines(adapter):
    output = 'not json\n{"role":"assistant","content":"有效。"}\n{broken'
    assert adapter._extract_assistant_text(output) == "有效。"


def test_extract_empty_output(adapter):
    assert adapter._extract_assistant_text("") == ""
    assert adapter._extract_assistant_text('{"role":"meta","type":"x"}') == ""


def test_cleanup_defaults(adapter):
    assert adapter._CLEANUP_MIN_CHARS >= 100   # 短指令不应触发整理
    assert adapter._CLEANUP_TIMEOUT_S > 0
    assert adapter._KIMI_CLI.endswith("kimi.exe")
