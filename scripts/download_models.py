#!/usr/bin/env python
"""把 MicToAgent 需要的三个模型下载到 models/（或 MICTOAGENT_MODELS）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

不依赖 venv。仓库根目录执行：
    py -3 scripts/download_models.py

已存在的文件/目录会跳过。GitHub Release 在国内可能较慢或失败，
失败时按 models/README.md 的链接自行下载，解压后目录名保持不变。
"""

from __future__ import annotations

import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "voice-bridge"))

from envutil import models_dir  # noqa: E402

UA = "MicToAgent-model-downloader"
ASR = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"

MODELS = (
    {
        "name": "ten-vad.onnx",
        "url": f"{ASR}/ten-vad.onnx",
        "kind": "file",
        "dest": "ten-vad.onnx",
    },
    {
        "name": "qwen3-asr 0.6B int8（精修）",
        "url": f"{ASR}/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2",
        "kind": "tar",
        "dest": "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25",
    },
    {
        "name": "SenseVoice-Small int8（初稿）",
        "url": f"{ASR}/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
        "kind": "tar",
        "dest": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "strip": True,
    },
)


def _progress(prefix: str):
    last = [-1]

    def hook(block_n: int, block_size: int, total: int) -> None:
        if total <= 0:
            return
        pct = min(100, int(block_n * block_size * 100 / total))
        if pct != last[0] and (pct % 5 == 0 or pct == 100):
            last[0] = pct
            mb = total / (1024 * 1024)
            print(f"\r  {prefix}  {pct:3d}%  ({mb:.0f} MB)", end="", flush=True)
            if pct == 100:
                print()

    return hook


def _download(url: str, path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    print(f"下载 {label}")
    print(f"  {url}")
    with urllib.request.urlopen(req) as resp, tmp.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        hook = _progress(label)
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            hook(got, 1, total if total else got)
    tmp.replace(path)


def _extract_tar_bz2(archive: Path, dest_dir: Path) -> None:
    print(f"解压 {archive.name}")
    kwargs = {}
    if hasattr(tarfile, "data_filter"):
        kwargs["filter"] = "data"
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(dest_dir.parent, **kwargs)


def _strip_sensevoice(root: Path) -> None:
    """生产只用 int8；fp32 和测试音频占空间。"""
    fp32 = root / "model.onnx"
    wavs = root / "test_wavs"
    if fp32.is_file():
        fp32.unlink()
        print(f"  已删 {fp32.name}（生产用 model.int8.onnx）")
    if wavs.is_dir():
        shutil.rmtree(wavs)
        print("  已删 test_wavs/")


def main() -> int:
    dest_root = models_dir()
    dest_root.mkdir(parents=True, exist_ok=True)
    print(f"模型目录：{dest_root}")
    for item in MODELS:
        dest = dest_root / item["dest"]
        if dest.exists():
            print(f"已有 {item['dest']}，跳过")
            continue
        if item["kind"] == "file":
            _download(item["url"], dest, item["name"])
            continue
        archive = dest_root / Path(item["url"]).name
        try:
            _download(item["url"], archive, item["name"])
            _extract_tar_bz2(archive, dest)
        finally:
            if archive.is_file():
                archive.unlink()
        if not dest.exists():
            print(f"解压后没找到 {dest.name}，请对照 models/README.md 手动放置", file=sys.stderr)
            return 1
        if item.get("strip"):
            _strip_sensevoice(dest)
    print("完成。目录名必须和 models/README.md 一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
