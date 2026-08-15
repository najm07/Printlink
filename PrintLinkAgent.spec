# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['agent\\main.py'],
    pathex=['agent'],
    binaries=[],
    datas=[],
    hiddenimports=['win32api', 'win32print', 'win32pipe', 'win32file', 'pywintypes'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'numpy', 'pandas', 'dateutil',
              'IPython', 'notebook', 'jupyter', 'zmq', 'pygments', 'tornado',
              'pydoc_data', 'test'],
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
    name='PrintLinkAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['installer\\assets\\icon.ico'],
)
