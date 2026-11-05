# Builds dist\LANDECK.exe (one file, asks for admin) and dist\LANDECK-onedir.zip.
#   powershell -ExecutionPolicy Bypass -File build.ps1
param(
    [switch]$SkipOnedir
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

# 1. virtualenv + dependencies
Step "Python environment"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { python -m venv .venv }
}
$py = (Resolve-Path ".venv\Scripts\python.exe").Path
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r requirements.txt pyinstaller --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# 2. icons
Step "Icons"
if (-not (Test-Path "assets\icon.ico") -or -not (Test-Path "web\icons\icon-192.png")) {
    & $py tools\make_icons.py
} else {
    Write-Host "already there (delete assets\icon.ico to regenerate)"
}

$common = @(
    "--noconfirm", "--clean", "--log-level", "WARN",
    "--name", "LANDECK",
    "--windowed",
    "--uac-admin",
    "--icon", "assets\icon.ico",
    "--version-file", "packaging\version.txt",
    "--add-data", "web;web",
    "--add-data", "assets;assets",
    "--collect-all", "uvicorn",
    "--collect-all", "websockets",
    "--collect-all", "zeroconf",
    "--collect-submodules", "pycaw",
    "--collect-submodules", "comtypes",
    "--collect-submodules", "winsdk.windows.media.control",
    "--collect-submodules", "winsdk.windows.storage.streams",
    "--collect-submodules", "winsdk.windows.foundation",
    "--hidden-import", "winsdk._winrt",
    "--hidden-import", "pystray._win32",
    "--hidden-import", "win32timezone",
    "--hidden-import", "win32ui",
    "--exclude-module", "tkinter",
    "--exclude-module", "playwright",
    "--exclude-module", "pytest"
)

# 3. one-file build
Step "PyInstaller (onefile)"
& $py -m PyInstaller @common --onefile --distpath dist --workpath build\onefile landeck.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller onefile build failed" }

# 4. one-folder build, zipped (starts faster, fewer antivirus false positives)
if (-not $SkipOnedir) {
    Step "PyInstaller (onedir)"
    & $py -m PyInstaller @common --onedir --distpath build\onedir-dist --workpath build\onedir landeck.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller onedir build failed" }
    $zip = "dist\LANDECK-onedir.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path "build\onedir-dist\LANDECK\*" -DestinationPath $zip -CompressionLevel Optimal
}

# 5. report
Step "Result"
foreach ($f in @("dist\LANDECK.exe", "dist\LANDECK-onedir.zip")) {
    if (Test-Path $f) {
        $item = Get-Item $f
        $hash = (Get-FileHash $f -Algorithm SHA256).Hash
        "{0,-26} {1,8:N1} MB  SHA-256 {2}" -f $item.Name, ($item.Length / 1MB), $hash
    }
}
