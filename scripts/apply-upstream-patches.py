"""apply-upstream-patches：自动打第三方依赖补丁（speech-to-kimi-code fork 原创）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 speech-to-kimi-code contributors

历史：本脚本曾维护 5 个 site-packages 补丁。项目转为真正的 fork 后，
speech_to_speech 包内的 4 个补丁（MPS 判断 / nltk 路径 / VAD 回退 /
funasr 跳过更新检查）已成为 src/ 里的正式源码改动（带 fork 注释标记），
venv 以可编辑方式安装本仓库 src/，不再丢失、无需重打。

现在脚本只负责**第三方库**的 1 个补丁（transformers，重装依赖后会丢失）：

    .venv\\Scripts\\python.exe scripts\\apply-upstream-patches.py          # 打补丁
    .venv\\Scripts\\python.exe scripts\\apply-upstream-patches.py --check  # 只检查不打

特性：
- 幂等：已打过的补丁显示 already，重复运行无副作用
- 补丁锚点找不到时报 error 并提示（上游版本变动导致，需人工核对文档）
- 默认作用于运行本脚本所用 Python 的 site-packages（请用项目的 .venv 运行）
- 字节级替换，保留原文件换行符（LF/CRLF）
"""

import argparse
import sys
import sysconfig
from pathlib import Path

# 每个补丁：name / 相对 site-packages 的路径 / 查找锚点（未打补丁时的原文）/
# 替换内容 / 已打标记（存在即视为已打）
# 注意：speech_to_speech 包内的改动已并入 src/ 正式源码（见 docs/upstream-patches.md），
# 这里只剩第三方库（transformers）的补丁。
PATCHES = [
    {
        "name": "transformers is_base_mistral 离线容错",
        "file": "transformers/tokenization_utils_base.py",
        "old": "        def is_base_mistral(model_id: str) -> bool:\n"
               "            model = model_info(model_id)",
        "new": "        def is_base_mistral(model_id: str) -> bool:\n"
               "            try:\n"
               "                model = model_info(model_id)\n"
               "            except Exception:\n"
               "                return False",
        "marker": "        def is_base_mistral(model_id: str) -> bool:\n"
                  "            try:",
    },
]


def _bytes(s: str, nl: str) -> bytes:
    return s.replace("\n", nl).encode("utf-8")


def apply_patch(site: Path, p: dict, check_only: bool) -> str:
    """返回状态：already / applied / would-apply / missing-file / error"""
    f = site / p["file"]
    if not f.exists():
        return "missing-file"
    data = f.read_bytes()
    nl = "\r\n" if b"\r\n" in data else "\n"
    marker, old, new = (_bytes(s, nl) for s in (p["marker"], p["old"], p["new"]))
    if marker in data:
        return "already"
    if old not in data:
        return "error"
    if check_only:
        return "would-apply"
    if data.count(old) > 1:
        return "error"  # 锚点不唯一，拒绝盲改
    f.write_bytes(data.replace(old, new, 1))
    return "applied"


def main() -> int:
    ap = argparse.ArgumentParser(description="应用第三方依赖（transformers）补丁")
    ap.add_argument("--check", action="store_true", help="只检查状态，不修改文件")
    ap.add_argument("--site-packages", type=Path, default=None,
                    help="指定 site-packages 目录（默认取当前 Python 环境的）")
    args = ap.parse_args()

    site = args.site_packages or Path(sysconfig.get_paths()["purelib"])
    if not site.is_dir():
        print(f"site-packages 目录不存在: {site}", file=sys.stderr)
        return 2
    print(f"目标 site-packages: {site}\n")

    rc = 0
    for p in PATCHES:
        status = apply_patch(site, p, args.check)
        icon = {"already": "✓ 已打过", "applied": "✓ 已应用",
                "would-apply": "○ 待应用", "missing-file": "✗ 文件缺失",
                "error": "✗ 锚点未找到"}[status]
        print(f"  [{icon:9s}] {p['name']}")
        if status == "missing-file":
            print(f"             {site / p['file']}（依赖未安装？）")
            rc = 1
        elif status == "error":
            print(f"             上游版本可能已变动，请对照 docs/upstream-patches.md 人工核对")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
