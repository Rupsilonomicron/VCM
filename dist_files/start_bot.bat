@echo off
rem === Start VCM (VoiceChatMover) ===
rem Uses the bundled Python runtime. No installation required.
cd /d "%~dp0"
set PYTHONUTF8=1

rem --- Open the GUI in the default browser ---
start "" http://127.0.0.1:8765

rem --- Run the app (discord bot + local web server) ---
rem Closing the GUI in the browser stops the app and this window closes too.
"python\python.exe" -m vcm.main

rem Keep the window open only if the app exited with an error.
if errorlevel 1 (
    echo.
    echo --- VCM exited with an error ---
    pause
)
