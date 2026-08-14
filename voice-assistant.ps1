# 语音助手一键开关（兼容入口）
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 MicToAgent contributors
#
# 实际的开关逻辑（启动向导 / 停止杀进程 / 防抖）都在
#   voice-bridge\voice-launcher.pyw
# 本脚本只是兼容入口：转发给 pythonw 执行，供手动调用或其他工具引用。
# 桌面快捷方式「MicToAgent」直接指向 pythonw.exe + voice-launcher.pyw，
# 不经过本脚本，因此启动时不会再闪现终端窗口。
#
# 快捷方式 = 只启动：未运行→启动向导（浅色进度卡片实时展示四个阶段：
# 适配器 → 依赖库 → 识别模型 → 连接目标会话；全部就绪后才
# 启动 mic-gate，托盘图标出现 = 真正可用）；已在运行→只弹提示卡。
# 停止走托盘图标右键「退出」（调 voice-launcher --stop 杀全部进程）。
# 诊断日志：仓库根目录 logs\*.log
# 本机路径：优先仓库根 .env 里的 MICTOAGENT_ROOT，否则用本脚本所在目录。

function Import-DotEnv([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Get-Content -LiteralPath $Path -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#')) { return }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { return }
        $key = $line.Substring(0, $eq).Trim()
        if ($key.StartsWith('export ')) { $key = $key.Substring(7).Trim() }
        $val = $line.Substring($eq + 1).Trim()
        if ($val.Length -ge 2 -and $val[0] -eq $val[-1] -and ($val[0] -eq '"' -or $val[0] -eq "'")) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        if ($key -and -not (Test-Path -LiteralPath "Env:$key")) {
            Set-Item -LiteralPath "Env:$key" -Value $val
        }
    }
}

$projDir = $PSScriptRoot
Import-DotEnv (Join-Path $projDir '.env')
if ($env:MICTOAGENT_ROOT) { $projDir = $env:MICTOAGENT_ROOT }

$bridgeDir  = Join-Path $projDir 'voice-bridge'
$pythonwExe = Join-Path $projDir '.venv\Scripts\pythonw.exe'
$launcher   = Join-Path $bridgeDir 'voice-launcher.pyw'

Start-Process -FilePath $pythonwExe -ArgumentList "`"$launcher`"" -WorkingDirectory $bridgeDir
exit 0
