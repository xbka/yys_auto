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


def _conda_binaries(*names):
    """
    收集 conda 环境 Library\\bin 下的 DLL。

    conda 的第三方 DLL 不放在 sys.prefix 根目录，PyInstaller 解析扩展模块
    的导入表时找不到，结果就打出一个「看着完整、一启动就崩」的包：
      - 缺 ffi-8.dll     → `_ctypes` 加载失败，报
        ImportError: DLL load failed while importing _ctypes
      - 缺 libssl/libcrypto → `_ssl` / `_hashlib` 加载失败
    这些扩展模块本身会被打包，但它们的依赖不会，所以这里显式声明。
    """
    bin_dir = os.path.join(sys.prefix, 'Library', 'bin')
    found = []
    for name in names:
        path = os.path.join(bin_dir, name)
        if os.path.isfile(path):
            found.append((path, '.'))
        else:
            print(f'[spec] 警告: 未找到 {path}，若打包后报 DLL 加载失败请检查环境')
    return found


a = Analysis(
    ['main.py'],
    pathex=[SPECPATH],
    binaries=_conda_binaries(
        'ffi-8.dll',                 # _ctypes
        'ffi-7.dll',                 # 旧版 libffi 命名
        'libssl-3-x64.dll',          # _ssl / _hashlib
        'libcrypto-3-x64.dll',       # 上面两个的依赖
        'libbz2.dll',                # _bz2
        'liblzma.dll',               # _lzma
        'libexpat.dll',              # pyexpat
        'libmpdec-4.dll',            # _decimal
    ),
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
    uac_admin=True,               # 清单写入 requireAdministrator：默认以管理员启动
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
