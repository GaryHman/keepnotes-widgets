@echo off
rem Removes the shortcuts and the .venv folder. Your notes stay in Google Keep;
rem the local cache/layout in %APPDATA%\KeepNotesWidgets and the saved login in
rem Windows Credential Manager are NOT removed (see README to remove them too).
setlocal
cd /d "%~dp0"
echo Closing KeepNotes Widgets is up to you: right-click the tray icon and choose Quit first.
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0shortcut.ps1" -Folder Programs -Remove
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0shortcut.ps1" -Folder Startup -Remove
if exist ".venv" (
  rmdir /s /q ".venv"
  echo Removed .venv
)
echo Done. Your notes are untouched (they live in Google Keep).
exit /b 0
