# Creates or removes the "KeepNotes Widgets" shortcut. Used by install.bat / uninstall.bat.
#   powershell -File shortcut.ps1 -Folder Programs|Startup [-Remove]
param(
    [Parameter(Mandatory = $true)][ValidateSet('Programs', 'Startup')][string]$Folder,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$appDir = $PSScriptRoot
$lnk = Join-Path ([Environment]::GetFolderPath($Folder)) 'KeepNotes Widgets.lnk'
if ($Remove) {
    if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk; Write-Host "Removed $lnk" }
    exit 0
}
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = Join-Path $appDir '.venv\Scripts\pythonw.exe'
$s.Arguments = '"' + (Join-Path $appDir 'keep_widgets.py') + '"'
$s.WorkingDirectory = $appDir
$s.IconLocation = Join-Path $appDir 'icon.ico'
$s.Description = 'KeepNotes Widgets - Google Keep notes on your desktop'
$s.Save()
Write-Host "Created $lnk"
