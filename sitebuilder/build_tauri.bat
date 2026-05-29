@echo off
REM Sentinel Sitebuilder desktop — release build (Windows).
REM
REM Pattern mirrors desktop/build_tauri.bat exactly, because Git Bash's
REM /usr/bin/link.exe shadows MSVC link.exe and the vcvars64 priming below
REM is the only reliable workaround. See memory
REM `feedback_tauri_windows_bash_link_shadow`.
REM
REM Outputs:
REM   src-tauri\target\release\sentinel-sitebuilder-desktop.exe   (bare exe)
REM   src-tauri\target\release\bundle\msi\*.msi                   (MSI installer)
REM   src-tauri\target\release\bundle\nsis\*-setup.exe            (NSIS installer)
REM
REM First build ~5-10 min (full release compile). Reruns ~30s.

setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >NUL
if errorlevel 1 (
    echo ABORT: vcvars64.bat failed. Install VS 2022 Build Tools with
    echo the C++ Build Tools workload.
    exit /b 1
)
set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
cd /d "%~dp0"

REM Kill any previously-launched dev/release exe so cargo can overwrite it.
REM (Forgetting to close = "os error 5: Access denied".)
taskkill /F /IM sentinel-sitebuilder-desktop.exe >NUL 2>&1

REM npm install runs only on first build (idempotent after).
if not exist node_modules (
    echo Installing @tauri-apps/cli ...
    call npm install --silent
)

npx tauri build
