# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — one file, both targets.

    pyinstaller TastyFwdFactor.spec        (or: python build.py)

Windows → ``dist/TastyFwdFactor.exe``, a single self-extracting binary.
macOS   → ``dist/TastyFwdFactor.app``, a bundle directory that ``build.py``
          then zips with ``ditto``. A .app is full of symlinks (every
          framework's ``Versions/Current``), so it must never be zipped with a
          tool that follows or drops them — see build.py.

The app writes nothing beside its own code: ``debug.log`` lives in
``~/.config/calendar-spread/`` with the rest of the user state (see applog.py),
because a onefile temp dir is deleted on exit and a signed .app bundle is
read-only. ``full.csv`` is bundled read-only and found via
``config.resource_path``, which knows about ``sys._MEIPASS``.
"""

import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

MACOS = sys.platform == 'darwin'

datas = [('full.csv', '.')]
# zoneinfo reads the tzdata package's data files at runtime; there is no import
# of them for the analyser to follow. market_clock's NY clock depends on this.
datas += collect_data_files('tzdata')
# keyring discovers its backends through entry points, i.e. through the
# distribution metadata — without it, KEYRING_AVAILABLE is False in the frozen
# build and credentials silently fall back to the JSON file.
datas += copy_metadata('keyring')

hiddenimports = [
    'keyring.backends.Windows',
    'keyring.backends.macOS',
    'keyring.backends.SecretService',
    'keyring.backends.chainer',
    'keyring.backends.fail',
]
# The SDK is imported lazily in places and pulls in pydantic models; sweep it.
hiddenimports += collect_submodules('tastytrade')

# Qt ships far more than this app touches, and PyInstaller will happily bundle
# a 400 MB WebEngine it never loads. Everything here is verified unused.
excludes = [
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
    'PySide6.QtQuick', 'PySide6.QtQml', 'PySide6.Qt3DCore', 'PySide6.QtMultimedia',
    'PySide6.QtBluetooth', 'PySide6.QtDesigner', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
    'PySide6.QtPositioning', 'PySide6.QtSerialPort', 'PySide6.QtSql', 'PySide6.QtTest',
    'shiboken6.support',
    'tkinter', 'matplotlib', 'IPython', 'notebook', 'pytest',
    'PyQt5', 'PyQt6',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

if MACOS:
    # onedir + BUNDLE. A onefile .app would unpack ~200 MB to /var/folders on
    # every launch, and Gatekeeper treats the extracted binary as a separate,
    # unsigned executable.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='TastyFwdFactor',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
        target_arch=None,          # native arch; set 'universal2' only with universal wheels
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name='TastyFwdFactor',
    )
    app = BUNDLE(
        coll,
        name='TastyFwdFactor.app',
        icon=None,
        bundle_identifier='com.calendarspread.tastyfwdfactor',
        info_plist={
            'CFBundleName':             'TastyFwdFactor',
            'CFBundleDisplayName':      'TastyFwdFactor',
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion':          '1.0.0',
            'NSHighResolutionCapable':  True,
            'LSMinimumSystemVersion':   '11.0',
            # Not a background agent: it owns a window and belongs in the Dock.
            'LSUIElement':              False,
            'NSRequiresAquaSystemAppearance': False,   # honour the user's dark mode
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='TastyFwdFactor',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        # UPX mangles Qt's DLLs often enough that it is not worth the megabytes.
        upx=False,
        runtime_tmpdir=None,
        console=False,             # GUI app: no console window behind it
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=None,
    )
