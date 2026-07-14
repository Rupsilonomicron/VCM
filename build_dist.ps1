# Build script for the VCM distribution package.
# Rebuilds <repo>\<HaiFuYou>\VCM and VCM.zip from the sources in this folder.
# Safe to run repeatedly; downloads the embeddable Python only when missing.
#
# NOTE: This file is intentionally pure ASCII. Windows PowerShell 5.1
# misreads UTF-8 scripts without BOM, so the Japanese folder name
# ("HaiFuYou" = distribution) is built from Unicode code points instead.

$ErrorActionPreference = "Stop"

$src = $PSScriptRoot
$distFolderName = [string][char]0x914D + [char]0x5E03 + [char]0x7528  # "HaiFuYou"
$distRoot = Join-Path (Split-Path $src -Parent) $distFolderName
# Staging folder for the assembled tree. Kept OUTSIDE $distRoot so the
# distribution folder ends up holding only the zip. Persisted between runs
# so the bundled Python download and dependency install stay cached.
$dist = Join-Path $src ".build\VCM"

# App version comes from vcm\__init__.py (single source of truth)
$initPy = Join-Path $src "vcm\__init__.py"
$versionMatch = Select-String -Path $initPy -Pattern '__version__\s*=\s*"([^"]+)"'
if (-not $versionMatch) { throw "__version__ not found in vcm\__init__.py" }
$appVersion = $versionMatch.Matches[0].Groups[1].Value
$zipPath = Join-Path $distRoot ("VCM_v{0}.zip" -f $appVersion)
$sitePackages = Join-Path $dist "python\Lib\site-packages"
$venvPython = Join-Path $src ".venv\Scripts\python.exe"

$pyVersion = "3.12.10"
$embedUrl = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip"

if (-not (Test-Path $venvPython)) {
    throw ".venv not found. Set it up first: py -m venv .venv ; .venv\Scripts\python.exe -m pip install -r requirements.txt"
}

New-Item -ItemType Directory -Force "$dist\vcm", "$dist\web" | Out-Null

# --- 1. Bundled Python runtime (download only when missing) -----------------
if (-not (Test-Path (Join-Path $dist "python\python.exe"))) {
    Write-Host "[build] downloading embeddable Python $pyVersion ..."
    $tmpZip = Join-Path $env:TEMP "python-embed-$pyVersion.zip"
    Invoke-WebRequest -Uri $embedUrl -OutFile $tmpZip
    Expand-Archive $tmpZip -DestinationPath (Join-Path $dist "python") -Force
    Remove-Item $tmpZip -Force
}

# Always (re)write the path config:
#   Lib\site-packages = bundled dependencies
#   ..                = app root, so "python -m vcm.main" works from anywhere
Set-Content -Path (Join-Path $dist "python\python312._pth") -Encoding Ascii -Value @(
    "python312.zip",
    ".",
    "Lib\site-packages",
    ".."
)

# --- 2. Dependencies (reinstall when requirements.txt changed or missing) ---
$reqSrc = Get-Content (Join-Path $src "requirements.txt") -Raw
$reqDistFile = Join-Path $dist "requirements.txt"
$reqDist = if (Test-Path $reqDistFile) { Get-Content $reqDistFile -Raw } else { "" }
if (($reqSrc -ne $reqDist) -or (-not (Test-Path $sitePackages))) {
    Write-Host "[build] installing dependencies into bundled site-packages ..."
    if (Test-Path $sitePackages) { Remove-Item $sitePackages -Recurse -Force }
    & $venvPython -m pip install -r (Join-Path $src "requirements.txt") --target $sitePackages --quiet
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

# --- 3. App files ------------------------------------------------------------
Write-Host "[build] copying app files ..."
Copy-Item "$src\vcm\*.py" "$dist\vcm\" -Force
Copy-Item "$src\web\*" "$dist\web\" -Force
Copy-Item "$src\requirements.txt" $dist -Force
Copy-Item "$src\CHANGELOG.md" $dist -Force
# Distribution-only files (recipient README, launcher for the bundled runtime)
Copy-Item "$src\dist_files\*" $dist -Force

# Drop files that no longer exist in the sources
Get-ChildItem "$dist\vcm" -File | Where-Object { -not (Test-Path (Join-Path "$src\vcm" $_.Name)) } | Remove-Item -Force
Get-ChildItem "$dist\web" -File | Where-Object { -not (Test-Path (Join-Path "$src\web" $_.Name)) } | Remove-Item -Force

# --- 4. Strip personal data and caches ---------------------------------------
@("config.json", ".env", "presets.json", "tts.json") |
    ForEach-Object { Join-Path $dist $_ } |
    Where-Object { Test-Path $_ } |
    Remove-Item -Force
Get-ChildItem $dist -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

# --- 5. Zip -------------------------------------------------------------------
Write-Host "[build] creating zip ..."
New-Item -ItemType Directory -Force $distRoot | Out-Null
# Drop zips from previous versions so only the current one remains
Get-ChildItem $distRoot -Filter "VCM*.zip" -File | Remove-Item -Force
Compress-Archive -Path $dist -DestinationPath $zipPath -Force

$folderMB = (Get-ChildItem $dist -Recurse -File | Measure-Object -Sum Length).Sum / 1MB
$zipMB = (Get-Item $zipPath).Length / 1MB
Write-Host ("[build] done: {0}" -f $zipPath)
Write-Host ("[build] folder {0:N1} MB / zip {1:N1} MB" -f $folderMB, $zipMB)
