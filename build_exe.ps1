$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$ffprobe = (Get-Command ffprobe -ErrorAction Stop).Source
$ffplay = (Get-Command ffplay -ErrorAction Stop).Source
$buildDependencies = Join-Path $projectDir ".build_deps"
$icon = Join-Path $projectDir "assets\icon.ico"
$pythonRoot = (python -c "import sys; print(sys.prefix)").Trim()
$hooks = Join-Path $projectDir "packaging\hooks"

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
        --additional-hooks-dir $hooks `
        --hidden-import tkinter `
        --hidden-import tkinter.ttk `
        --hidden-import tkinter.messagebox `
        --add-binary "$pythonRoot\DLLs\_tkinter.pyd;." `
        --add-binary "$pythonRoot\DLLs\tcl86t.dll;." `
        --add-binary "$pythonRoot\DLLs\tk86t.dll;." `
        --add-data "$pythonRoot\tcl\tcl8.6;_tcl_data" `
        --add-data "$pythonRoot\tcl\tk8.6;_tk_data" `
        --add-binary "$ffmpeg;." `
        --add-binary "$ffprobe;." `
        --add-binary "$ffplay;." `
        --add-data "assets/icon.ico;assets" `
        --add-data "assets/note.png;assets" `
        main.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

Write-Host "Built: $projectDir\dist\FlexScale.exe"
