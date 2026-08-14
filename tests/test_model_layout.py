"""下载脚本里的模型目录名必须和 voice-core 写死的一致。"""

import importlib.util
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
BRIDGE = PROJ / "voice-bridge"
sys.path.insert(0, str(BRIDGE))

from envutil import models_dir  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_download_script_dest_matches_voice_core():
    core = _load("voice_core_layout", BRIDGE / "voice-core.py")
    dl = _load("download_models", PROJ / "scripts" / "download_models.py")
    dests = {item["dest"] for item in dl.MODELS}
    assert dests == {
        "ten-vad.onnx",
        core.QWEN3_ASR_DIR.name,
        core.SENSE_VOICE_DIR.name,
    }
    assert core.TEN_VAD_MODEL.name == "ten-vad.onnx"
    assert core.TEN_VAD_MODEL.parent == models_dir()
