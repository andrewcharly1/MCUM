# MCUM one-command installer (Windows).
# Usage:  irm https://raw.githubusercontent.com/andrewcharly1/MCUM/main/install.ps1 | iex
$ErrorActionPreference = "Stop"

$repo = "https://github.com/andrewcharly1/MCUM.git"
# Permanent install location (override with $env:MCUM_HOME). Folder MUST be "MCUM".
$dest = if ($env:MCUM_HOME) { $env:MCUM_HOME } else { Join-Path $HOME "MCUM" }

Write-Host "==> Installing MCUM to $dest" -ForegroundColor Cyan

foreach ($tool in @("git", "node", "npm")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is required but was not found on PATH. Install it and re-run."
    }
}
if (-not (Get-Command "py" -ErrorAction SilentlyContinue) -and
    -not (Get-Command "python" -ErrorAction SilentlyContinue)) {
    throw "Python is required but was not found on PATH."
}

if (Test-Path (Join-Path $dest ".git")) {
    Write-Host "==> Updating existing clone" -ForegroundColor Cyan
    git -C $dest pull --ff-only
} else {
    git clone --depth 1 $repo $dest
}

Push-Location $dest
try {
    Write-Host "==> npm install" -ForegroundColor Cyan
    npm install
    Write-Host "==> mcum init" -ForegroundColor Cyan
    node bin/mcum.mjs init
} finally {
    Pop-Location
}

Write-Host "`n==> Done. MCUM installed at $dest" -ForegroundColor Green
Write-Host "    Restart your agent to load the 30 mcum_* tools."
Write-Host "    Register another project:  py $dest\mcum_install.py register --workspace <path>"
