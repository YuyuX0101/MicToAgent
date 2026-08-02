# 语音助手一键开关（兼容入口）
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 speech-to-kimi-code contributors（本文件为 speech-to-kimi-code fork 原创）
#
# 实际的开关逻辑（启动向导 / 停止杀进程 / 防抖）都在
#   voice-bridge\voice-launcher.pyw
# 本脚本只是兼容入口：转发给 pythonw 执行，供手动调用或其他工具引用。
# 桌面快捷方式「语音助手」直接指向 pythonw.exe + voice-launcher.pyw，
# 不经过本脚本，因此启动时不会再闪现终端窗口。
#
# 快捷方式 = 只启动：未运行→启动向导（浅色进度卡片实时展示四个阶段：
# 适配器 → 语音识别模型 → 语音合成模型 → 连接语音总管；全部就绪后才
# 启动 mic-gate，托盘图标出现 = 真正可用）；已在运行→只弹提示卡。
# 停止走托盘图标右键「退出」（调 voice-launcher --stop 杀全部进程）。
# 诊断日志：E:\workspace\speech-to-kimi-code\logs\*.log

$projDir    = 'E:\workspace\speech-to-kimi-code'
$bridgeDir  = Join-Path $projDir 'voice-bridge'
$pythonwExe = Join-Path $projDir '.venv\Scripts\pythonw.exe'
$launcher   = Join-Path $bridgeDir 'voice-launcher.pyw'

Start-Process -FilePath $pythonwExe -ArgumentList "`"$launcher`"" -WorkingDirectory $bridgeDir
exit 0
