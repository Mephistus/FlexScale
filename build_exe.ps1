$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$ffprobe = (Get-Command ffprobe -ErrorAction Stop).Source
$ffplay = (Get-Command ffplay -ErrorAction Stop).Source
$buildDependencies = Join-Path $projectDir ".build_deps"
$icon = Join-Path $projectDir "assets\icon.ico"

if (-not (Test-Path $icon)) {
    throw "Missing executable icon: $icon"
}

if (-not (Test-Path (Join-Path $buildDependencies "PyInstaller"))) {
    python -m pip install --target $buildDependencies pyinstaller
}

$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $buildDependencies
    python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name FlexScale `
        --icon $icon `
        --add-binary "$ffmpeg;." `
        --add-binary "$ffprobe;." `
        --add-binary "$ffplay;." `
        --add-data "assets/note.png;assets" `
        main.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

Write-Host "Built: $projectDir\dist\FlexScale.exe"
