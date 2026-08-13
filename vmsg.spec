# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for VMSG.

Tracked in git deliberately. Previously the build passed flags on the command
line and let PyInstaller regenerate a throwaway spec, which meant the packaging
of an official artifact was defined by an argv string rather than by a reviewed
file - and changes to it could not be diffed or code-owned.

Build with:
    python build_binary.py            # runs pre-flight checks first
    pyinstaller --noconfirm --clean vmsg.spec    # raw, no checks

Two things here are easy to get wrong and fail *silently* at runtime:

  datas         The whole dashboard is bundled from static/. If this mapping
                breaks, the gateway still starts and the API still answers - the
                dashboard just 404s. tools/verify_frozen_build.py checks it.

  hiddenimports PyInstaller resolves imports statically. Anything reached only
                by name (uvicorn's protocol/loop autoloaders, pyvisa backends)
                must be listed or it is missing from the bundle. VMSG's VXI-11
                stack is imported inside main(); PyInstaller does follow
                function-level imports, and the frozen-build gate proves it
                each release rather than trusting it.
"""

import os

block_cipher = None

a = Analysis(
    ['vmsg.py'],
    pathex=[],
    binaries=[],
    # The dashboard. See the note above - a break here is silent.
    datas=[('static', 'static')],
    hiddenimports=[
        # VISA backends, loaded by name at runtime.
        'pyvisa_py',
        'serial',
        'usb',
        # uvicorn resolves these through its "auto" indirection, so static
        # analysis cannot see them.
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # Imported inside main(). Listed explicitly so the bundle does not
        # depend on PyInstaller's function-level import analysis.
        'vmsg_core.vxi11_emulator',
        'vmsg_core.vxi11_lxi_emulator',
        'vmsg_core.vxi11_bridge',
        'vmsg_core.mdns',
        'vmsg_core.diagnostics',
        'vmsg_core.netutil',
        'vmsg_core.apiauth',
        'vmsg_core.crashlog',
        'vmsg_core.paths',
        'vmsg_core.version',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Not used by VMSG; excluding keeps the artifact smaller and avoids
        # dragging in a GUI toolchain on build machines that happen to have one.
        'tkinter',
        'PySide6',
        'PyQt5',
        'matplotlib',
    ],
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
    name='vmsg',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is off: it offers little on an already-compressed onefile bundle and
    # is a common false-positive trigger for antivirus, which matters for an
    # unsigned artifact users download from a release page.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join('static', 'favicon.ico') if os.path.exists(
        os.path.join('static', 'favicon.ico')) else None,
)
