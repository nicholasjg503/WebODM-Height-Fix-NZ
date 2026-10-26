# Builds dist\NZHeightFix\ (the app) and, if Inno Setup 6 is installed,
# dist\NZHeightFix-Setup-<version>.exe (the installer). Used locally and by GitHub Actions.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (Select-String -Path nz_height_fix.py -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Building NZ Height Fix $version"

$uv = & (Join-Path $PSScriptRoot 'get-uv.ps1')

# Bundle the NZGeoid2016 grid so geoid mode works offline
New-Item -ItemType Directory -Force build | Out-Null
$grid = 'build\nz_linz_nzgeoid2016.tif'
if (-not (Test-Path $grid)) {
    Write-Host 'Downloading NZGeoid2016 grid...'
    Invoke-WebRequest 'https://cdn.proj.org/nz_linz_nzgeoid2016.tif' -OutFile $grid
}

# uv and PyInstaller print progress on stderr; judge them by exit code, not by stderr
$ErrorActionPreference = 'Continue'
& $uv run --no-project --python 3.13 --with-requirements requirements.txt --with pyinstaller `
    pyinstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\nz_height_fix.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
$ErrorActionPreference = 'Stop'

# Smoke test: the frozen app must load GDAL, PROJ, LAZ support and the bundled geoid grid
$p = Start-Process dist\NZHeightFix\NZHeightFix.exe -ArgumentList '--selftest' -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "Built app failed its self-test (exit code $($p.ExitCode))" }
Write-Host 'Built app passed its self-test.'

$iscc = @(
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc /Q "/DAppVersion=$version" packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
    Write-Host "Installer: dist\NZHeightFix-Setup-$version.exe"
} else {
    Write-Host 'Inno Setup 6 not found - skipped the installer. The app is in dist\NZHeightFix\.'
}
