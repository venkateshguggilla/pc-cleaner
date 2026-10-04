@echo off
rem Build dist\PCCleaner.exe, then the installer installer\Output\PCCleaner-Setup-<version>.exe
rem Needs Inno Setup 6 (winget install JRSoftware.InnoSetup).
cd /d "%~dp0"
call "%~dp0build.bat" || exit /b 1

set ISCC=
for %%P in ("%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe") do (
  if exist %%P set ISCC=%%P
)
if not defined ISCC (echo Inno Setup 6 not found - install it with: winget install JRSoftware.InnoSetup & exit /b 1)
%ISCC% installer\PCCleaner.iss
