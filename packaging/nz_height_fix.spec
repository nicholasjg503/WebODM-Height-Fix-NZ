# PyInstaller spec - builds dist\WebODMHeightFixNZ\WebODMHeightFixNZ.exe (one folder, no console).
# Run through scripts\build.ps1, which downloads the geoid grid into build\ first.
import os

from PyInstaller.utils.hooks import collect_all

root = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(SPECPATH, "icon.ico"), "."),
    (os.path.join(root, "build", "nz_linz_nzgeoid2016.tif"), "."),  # works offline for geoid mode
]
binaries, hiddenimports = [], []
for pkg in ("rasterio", "pyproj", "laspy", "lazrs", "certifi", "ezdxf"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(root, "nz_height_fix.py")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "IPython", "pytest", "PIL"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WebODMHeightFixNZ",
    icon=os.path.join(SPECPATH, "icon.ico"),
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="WebODMHeightFixNZ")
