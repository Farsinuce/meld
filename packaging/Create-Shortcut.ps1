<#
.SYNOPSIS
    Put ArnisXL on the desktop and in the Start menu.

.DESCRIPTION
    ArnisXL ships as a folder you extract anywhere - there is no installer, so nothing has
    registered it with the shell. This script does the one thing an installer would have been
    wanted for: shortcuts with the right icon, pointing at wherever you actually put the folder.

    Run it again after moving the folder; a .lnk stores an absolute path and will not follow.

.PARAMETER Startup
    Also start ArnisXL when you log in. It opens straight to the tray, no window.

.PARAMETER Remove
    Delete the shortcuts this script created.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\Create-Shortcut.ps1
    powershell -ExecutionPolicy Bypass -File packaging\Create-Shortcut.ps1 -Startup
#>
[CmdletBinding()]
param(
    [switch]$Startup,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$AppName = 'ArnisXL'

# The script lives in packaging\ inside the app folder; the exe is one level up. Falls back to
# the source layout so this works from a checkout too, before anything has been frozen.
$Root = Split-Path -Parent $PSScriptRoot
$Exe = Join-Path $Root 'ArnisXL.exe'
$Arguments = ''
$IconPath = Join-Path $Root 'assets\icons\arnisxl.ico'

if (-not (Test-Path $Exe)) {
    $PyW = Join-Path $Root '.venv\Scripts\pythonw.exe'
    $Script = Join-Path $Root 'arnisxl.py'
    if ((Test-Path $PyW) -and (Test-Path $Script)) {
        # pythonw.exe, not python.exe: the w build has no console, so the source checkout
        # behaves like the packaged app - tray only, no black window in the taskbar.
        $Exe = $PyW
        $Arguments = "`"$Script`""
    } else {
        throw "Could not find ArnisXL.exe or a source checkout under $Root"
    }
}

$Desktop = [Environment]::GetFolderPath('Desktop')
$StartMenu = Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'Microsoft\Windows\Start Menu\Programs'
$StartupDir = [Environment]::GetFolderPath('Startup')

$Targets = @(
    (Join-Path $Desktop "$AppName.lnk"),
    (Join-Path $StartMenu "$AppName.lnk")
)
if ($Startup) { $Targets += (Join-Path $StartupDir "$AppName.lnk") }

if ($Remove) {
    foreach ($t in @((Join-Path $Desktop "$AppName.lnk"),
                     (Join-Path $StartMenu "$AppName.lnk"),
                     (Join-Path $StartupDir "$AppName.lnk"))) {
        if (Test-Path $t) { Remove-Item $t -Force; Write-Host "removed $t" }
    }
    return
}

$Shell = New-Object -ComObject WScript.Shell
foreach ($target in $Targets) {
    $dir = Split-Path -Parent $target
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $lnk = $Shell.CreateShortcut($target)
    $lnk.TargetPath = $Exe
    if ($Arguments) { $lnk.Arguments = $Arguments }
    $lnk.WorkingDirectory = $Root
    $lnk.Description = 'ArnisXL - real-world maps into Minecraft worlds'
    if (Test-Path $IconPath) { $lnk.IconLocation = $IconPath }
    $lnk.Save()
    Write-Host "created $target"
}

Write-Host ''
Write-Host "$AppName starts in the notification area (the ^ chevron next to the clock)."
Write-Host 'Windows 11 hides new tray icons there by default - drag it out to pin it.'
