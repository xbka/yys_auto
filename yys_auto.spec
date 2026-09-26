# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置

用法:
    $env:YYS_APP_VERSION='1.1'
    pyinstaller yys_auto.spec --distpath ../yys_auto_app

输出目录: yys_auto_app/yys_auto_v<YYS_APP_VERSION>/
版本号由环境变量 YYS_APP_VERSION 决定，未设置时使用 'dev'。
推荐直接用打包脚本: .\\build.ps1 -Version 1.1
"""

import os
import sys

_VERSION = os.environ.get('YYS_APP_VERSION', 'dev')

a = Analysis(
    ['main.py'],
    pathex=[SPECPATH],
    binaries=[],
    datas=[
        # 前端静态文件 → 打包到 _MEIPASS/frontend/web/
        ('frontend/web', 'frontend/web'),
    ],
    hiddenimports=[
        # Eel 框架
        'eel',
        'eel.browsers',
        'eel.chrome',
        # WebSocket 依赖
        'bottle',
        'bottle_websocket',
        'gevent',
        'gevent.websocket',
        'gevent._socket3',
        'gevent._ssl3',
        'gevent._util',
        'gevent.event',
        'gevent.hub',
        # 图像处理
        'cv2',
        'numpy',
        'PIL',
        'PIL.Image',
        'PIL.ImageGrab',
        'matplotlib',
        'matplotlib.backends.backend_agg',
        # 键鼠模拟
        'pyautogui',
        'pyscreeze',
        # 键盘监听
        'keyboard',
        'keyboard._winkeyboard',
        # Windows API
        'win32api',
        'win32gui',
        'win32con',
        'win32ui',
        'win32clipboard',
        'pywintypes',
        'pythoncom',
        # 队列
        'queue',
        # ctypes 相关
        'ctypes',
        'ctypes.wintypes',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='yys_auto',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                # 不显示控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=f'yys_auto_v{_VERSION}',
)
