# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 tsn-auto 工作台打成一个 exe。

    pyinstaller --noconfirm --clean route_studio.spec

产物：dist/<TSN_EXE_NAME>.exe（默认 tsn-auto-lite；名含 full = 完整版）。
静态资源 static/ 打进包内（运行时在 _MEIPASS/static，app.py 会把 server.STATIC 指过去）。
"""
import os

HERE = os.path.dirname(os.path.abspath(SPEC)) if "SPEC" in globals() else os.getcwd()

HIDDEN = [
    # 延迟导入（写在函数里的 import），静态分析容易漏，显式点名
    "mock_boxkj", "server", "flow", "v2client", "tracklib", "envlib",
    "device_profile", "device_catalog", "w8b_sign_probe", "v2_chain", "replay",
    "webview.platforms.winforms", "clr_loader", "pythonnet",
]

a = Analysis(
    [os.path.join(HERE, "app.py")],
    pathex=[HERE],
    binaries=[],
    datas=[(os.path.join(HERE, "static"), "static")],
    hiddenimports=HIDDEN,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["PIL", "tkinter", "matplotlib", "numpy", "pandas", "scipy", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=os.environ.get("TSN_EXE_NAME", "tsn-auto-lite"),
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    runtime_tmpdir=None, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
