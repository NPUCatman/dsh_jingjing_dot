param(
    [string]$ShortcutName = '小鲸鱼云端管理.lnk'
)

$ErrorActionPreference = 'Stop'
$managerDirectory = $PSScriptRoot
$launcherPath = Join-Path $managerDirectory 'start-manager.bat'
if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw '找不到 start-manager.bat，无法创建快捷方式。'
}
$desktopDirectory = [Environment]::GetFolderPath('DesktopDirectory')
$shortcutPath = Join-Path $desktopDirectory $ShortcutName
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcherPath
$shortcut.WorkingDirectory = (Split-Path -Parent $managerDirectory)
$shortcut.Description = '启动小鲸鱼 SSH 隧道和本地管理面板；关闭窗口自动断开连接'
$shortcut.WindowStyle = 1
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,17"
$shortcut.Save()
Write-Host "已创建桌面快捷方式：$shortcutPath"
