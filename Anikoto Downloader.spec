# -*- mode: python ; coding: utf-8 -*-
import os

datas_list = [('icon.ico', '.')]
if os.path.exists('assets'):
    datas_list.append(('assets', 'assets'))
if os.path.exists('ffmpeg'):
    datas_list.append(('ffmpeg', 'ffmpeg'))

a = Analysis(
    ['anikoto_qt.py'],
    pathex=[],
    binaries=[],
    datas=datas_list,
    hiddenimports=[
        'yt_dlp.compat._legacy',
        'yt_dlp.compat._deprecated',
        'yt_dlp.utils._legacy',
        'yt_dlp.utils._deprecated',
        'Crypto',
        'requests',
        'urllib3',
        'mutagen',
        'brotli',
        'certifi',
        'secretstorage',
        'curl_cffi',
        'PySide6',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'customtkinter'],
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
    name='Anikoto Downloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
