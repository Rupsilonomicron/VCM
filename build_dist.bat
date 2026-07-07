@echo off
rem === Build the VCM distribution package ===
rem Copies the sources into the distribution folder, refreshes the
rem bundled dependencies when needed, and recreates VCM.zip.
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_dist.ps1"

if errorlevel 1 (
    echo.
    echo --- Build FAILED ---
) else (
    echo.
    echo --- Build finished ---
)
pause
