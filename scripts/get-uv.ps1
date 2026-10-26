# Returns the path to uv.exe, downloading it into .tools\uv the first time.
# uv then manages a private Python 3.13 under .tools too, so the PC's own Python is never used.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root '.tools'
$uv = Join-Path $tools 'uv\uv.exe'

if (-not (Test-Path $uv)) {
    Write-Host 'First run: downloading uv (small Python manager)...'
    [Net.ServicePointManager]::SecurityProtocol = 'Tls12'
    $zip = Join-Path $env:TEMP 'uv-nzhf.zip'
    $tmp = Join-Path $env:TEMP 'uv-nzhf'
    Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive $zip -DestinationPath $tmp
    New-Item -ItemType Directory -Force (Split-Path $uv) | Out-Null
    Get-ChildItem $tmp -Recurse -Filter 'uv*.exe' | Copy-Item -Destination (Split-Path $uv)
    Remove-Item $zip, $tmp -Recurse -Force
}

$env:UV_PYTHON_INSTALL_DIR = Join-Path $tools 'python'
$env:UV_CACHE_DIR = Join-Path $tools 'cache'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$uv
