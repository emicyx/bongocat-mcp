# -*- mode: python ; coding: utf-8 -*-
"""bongocat-mcp 打包配置：onedir 单文件夹（启动快、杀软误报率低、含全部依赖）。

产出 exe 为控制台程序（MCP stdio server 模式需要标准输入输出），
双击运行时 dashboard 入口会自动转入隐藏后台进程并打开浏览器，不留黑窗。
"""

import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        # 按源码中的相对路径放置，dashboard.py 的 WEB_DIR 解析无需改动
        (os.path.join(ROOT, "web", "index.html"), "web"),
        (os.path.join(ROOT, "config.example.json"), "."),
        # mver-mirror.py 模块名带连字符无法 import，launcher 以 runpy 执行数据文件
        (os.path.join(ROOT, "mver-mirror.py"), "."),
    ],
    hiddenimports=[
        # uvicorn 按字符串动态选择 loop/protocol 实现，需整包收进
        *collect_submodules("uvicorn"),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 明确用不到的大块依赖，减小体积（Pillow 仅测试脚本使用）
        "PIL",
        "pillow_heif",
        "pytest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="bongocat-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="bongocat-mcp",
)
