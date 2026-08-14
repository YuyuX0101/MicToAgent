# 在当前用户桌面创建 MicToAgent 快捷方式（无终端窗口）。
# 在仓库根目录执行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create-desktop-shortcut.ps1

$projDir = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $projDir ".venv\Scripts\pythonw.exe"
$launcher = Join-Path $projDir "voice-bridge\voice-launcher.pyw"
$icon = Join-Path $projDir "voice-assistant.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "MicToAgent.lnk"

if (-not (Test-Path -LiteralPath $pythonw)) {
    Write-Error "找不到 $pythonw 。请先在仓库根创建 .venv 并安装 requirements.txt。"
    exit 1
}
if (-not (Test-Path -LiteralPath $launcher)) {
    Write-Error "找不到 $launcher"
    exit 1
}

$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "`"$launcher`""
$lnk.WorkingDirectory = Join-Path $projDir "voice-bridge"
$lnk.Description = "MicToAgent 语音助手一键启动"
if (Test-Path -LiteralPath $icon) {
    $lnk.IconLocation = "$icon,0"
}
$lnk.WindowStyle = 1
$lnk.Save()
Write-Host "已创建 $lnkPath"
