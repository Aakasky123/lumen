import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


desktop_dir = Path(SPECPATH).resolve()
repo_root = desktop_dir.parents[1]
api_root = repo_root / "apps" / "api"

datas = [
    (str(api_root / "app" / "agent_eval" / "local_pages"), "app/agent_eval/local_pages"),
]

# Platform-specific imports: pywinauto and the win32 extensions exist only on Windows,
# and the PyObjC frameworks (Quartz, Vision, ScreenCaptureKit, ...) only on macOS.
PLATFORM_HIDDEN_IMPORTS = []
if sys.platform == "win32":
    PLATFORM_HIDDEN_IMPORTS = collect_submodules("pywinauto") + [
        "comtypes",
        "pythoncom",
        "win32api",
        "win32con",
        "win32gui",
        "win32process",
    ]
elif sys.platform == "darwin":
    for package in (
        "objc",
        "Quartz",
        "ApplicationServices",
        "Cocoa",
        "Vision",
        "ScreenCaptureKit",
    ):
        PLATFORM_HIDDEN_IMPORTS += collect_submodules(package)

hiddenimports = sorted(
    set(
        collect_submodules("app")
        + collect_submodules("uvicorn")
        + PLATFORM_HIDDEN_IMPORTS
        + [
            "multipart",
            "pydantic_settings",
            "sqlalchemy.dialects.sqlite",
        ]
    )
)

a = Analysis(
    ["api_sidecar.py"],
    pathex=[str(api_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="lumen-api",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
