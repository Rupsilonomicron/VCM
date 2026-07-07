@echo off
rem === Start VCM (VoiceChatMover) ===
rem Uses the bundled Python runtime. No installation required.
cd /d "%~dp0"
set PYTHONUTF8=1

rem --- Run the app (discord bot + local web server) ---
rem The app opens the GUI in the browser once the server is ready.
rem Closing the GUI in the browser stops the app and this window closes too.
"python\python.exe" -m vcm.main

rem Keep the window open only if the app exited with an error.
if errorlevel 1 (
    echo.
    echo --- VCM exited with an error ---
    pause
)
