"""check-updates：用国内镜像检查本项目核心依赖是否有新版本（speech-to-kimi-code fork 原创）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 speech-to-kimi-code contributors

funasr 自带的启动时更新检查写死请求 pypi.org（境外、无超时），已通过补丁 5 禁用。
本脚本提供按需手动检查：查询国内 PyPI 镜像的简单索引页，解析出最新发布版本，
与当前 .venv 中安装的版本对比。只读不写，不修改任何已安装的包。

用法：
    .venv\\Scripts\\python.exe scripts\\check-updates.py
"""

import importlib.metadata
import re
import sys

import requests
from packaging import version

# 国内 PyPI 镜像（简单索引 PEP 503），按优先级尝试
MIRRORS = [
    "https://mirrors.aliyun.com/pypi/simple/{pkg}/",
    "https://pypi.tuna.tsinghua.edu.cn/simple/{pkg}/",
]

# 本项目核心依赖（PyPI 包名）
PACKAGES = [
    "speech-to-speech",
    "funasr",
    "modelscope",
    "transformers",
    "torch",
]


def latest_version(pkg: str) -> version.Version | None:
    """从国内镜像的简单索引页解析最新正式版本号。"""
    # wheel/sdist 文件名中的包名：连字符归一化为下划线
    normalized = pkg.replace("-", "_")
    pattern = re.compile(
        rf"{re.escape(normalized)}-([0-9][A-Za-z0-9.]*?)(?:\.tar\.gz|\.zip|-py|-cp)",
        re.IGNORECASE,
    )
    for mirror in MIRRORS:
        try:
            r = requests.get(mirror.format(pkg=pkg), timeout=5)
            if r.status_code != 200:
                continue
            candidates = []
            for m in pattern.finditer(r.text):
                try:
                    v = version.parse(m.group(1))
                except version.InvalidVersion:
                    continue
                if not v.is_prerelease:
                    candidates.append(v)
            if candidates:
                return max(candidates)
        except requests.RequestException:
            continue
    return None


def main() -> int:
    print(f"{'包':<20}{'当前版本':<14}{'最新版本':<14}状态")
    print("-" * 60)
    rc = 0
    for pkg in PACKAGES:
        try:
            current = version.parse(importlib.metadata.version(pkg))
        except importlib.metadata.PackageNotFoundError:
            print(f"{pkg:<20}{'未安装':<14}")
            continue
        latest = latest_version(pkg)
        if latest is None:
            print(f"{pkg:<20}{current!s:<14}{'查询失败':<14}镜像不可达？")
            rc = 1
        elif latest > current:
            print(f"{pkg:<20}{current!s:<14}{latest!s:<14}↑ 可升级")
        else:
            print(f"{pkg:<20}{current!s:<14}{latest!s:<14}✓ 已是最新")
    print("\n升级前请先看 docs/upstream-patches.md：重装依赖会丢失上游补丁，"
          "升级后需重跑 scripts/apply-upstream-patches.py 并核对锚点。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
