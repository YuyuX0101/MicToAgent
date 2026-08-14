"""envutil：.env 加载与路径解析。"""

import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
BRIDGE = PROJ / "voice-bridge"
sys.path.insert(0, str(BRIDGE))

from envutil import apply_dotenv, models_dir, project_root  # noqa: E402


def test_apply_dotenv_does_not_override_existing(monkeypatch, tmp_path):
    monkeypatch.setenv("MICTOAGENT_TEST_KEY", "existing")
    p = tmp_path / ".env"
    p.write_text(
        "MICTOAGENT_TEST_KEY=fromfile\nMICTOAGENT_TEST_OTHER=yes\n",
        encoding="utf-8",
    )
    try:
        apply_dotenv(p)
        assert os.environ["MICTOAGENT_TEST_KEY"] == "existing"
        assert os.environ["MICTOAGENT_TEST_OTHER"] == "yes"
    finally:
        os.environ.pop("MICTOAGENT_TEST_OTHER", None)


def test_project_root_without_env(monkeypatch):
    monkeypatch.delenv("MICTOAGENT_ROOT", raising=False)
    assert project_root() == PROJ.resolve()


def test_project_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MICTOAGENT_ROOT", str(tmp_path))
    assert project_root() == tmp_path.resolve()
    assert models_dir() == (tmp_path / "models").resolve()


def test_models_dir_env_override(monkeypatch, tmp_path):
    models = tmp_path / "custom-models"
    monkeypatch.setenv("MICTOAGENT_MODELS", str(models))
    assert models_dir() == models.resolve()
