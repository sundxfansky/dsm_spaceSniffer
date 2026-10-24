# PyInstaller spec for the Windows launcher. Build with windows\build.ps1.
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent

hiddenimports = (
    collect_submodules("app")
    + collect_submodules("uvicorn")
    + ["pystray._win32"]
)

a = Analysis(
    [str(root / "windows" / "launcher.py")],
    pathex=[str(root)],
    datas=[(str(root / "app" / "static"), "app/static")],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DSMStorageInspector",
    console=False,
    icon=None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="DSMStorageInspector", upx=False)
