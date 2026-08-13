# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


ROOT = Path(SPEC).resolve().parent  # noqa: F821 - injected by PyInstaller
datas = [
    (str(ROOT / "THIRD_PARTY_NOTICES.txt"), "osr_updater"),
    (str(ROOT / "licenses"), "osr_updater/licenses"),
]
build_info = os.environ.get("OSR_UPDATER_BUILD_INFO")
if not build_info:
    raise SystemExit("OSR_UPDATER_BUILD_INFO is required")
datas.append((build_info, "osr_updater"))
datas += collect_data_files("esptool")
datas += copy_metadata("esptool", recursive=True)

analysis = Analysis(  # noqa: F821 - injected by PyInstaller
    [str(ROOT / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=(
        collect_submodules("esptool")
        + collect_submodules("serial")
        + ["tkinter", "tkinter.ttk"]
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["http.server", "webbrowser"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)  # noqa: F821 - injected by PyInstaller

executable = EXE(  # noqa: F821 - injected by PyInstaller
    pyz,
    analysis.scripts,
    [],
    name="osr-updater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    exclude_binaries=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
bundle = COLLECT(  # noqa: F821 - injected by PyInstaller
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="osr-updater",
)
