# -*- mode: python ; coding: utf-8 -*-
# Spec de PyInstaller para CoClody: un solo ejecutable de consola.
#
#   pyinstaller --clean --noconfirm coclody.spec
#
# - El punto de entrada (run_coclody.py) está fuera del paquete; ver su docstring.
# - Los backends de keyring se importan dentro de una función según la
#   plataforma; se declaran explícitamente para no depender del análisis.
# - Sin UPX: comprimir el binario aumenta los falsos positivos de antivirus.
# - No se empaqueta ningún archivo de datos propio: el SQL vive en el código.

import sys

hidden = ["keyring.backends.macOS"] if sys.platform == "darwin" else []
if sys.platform == "win32":
    hidden.append("keyring.backends.Windows")

a = Analysis(
    ["run_coclody.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "pytest", "_pytest", "PyInstaller"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="coclody",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
