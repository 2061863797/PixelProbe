# -*- mode: python ; coding: utf-8 -*-
"""PixelProbe 独立 CLI 的 PyInstaller 构建定义。"""

from pathlib import Path


项目根目录 = Path(SPECPATH).parent

分析 = Analysis(
    [str(项目根目录 / "src" / "pixelprobe" / "__main__.py")],
    pathex=[str(项目根目录 / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["cv2", "zarr", "mcp"],
    noarchive=False,
    optimize=0,
)

归档 = PYZ(分析.pure)

可执行文件 = EXE(
    归档,
    分析.scripts,
    分析.binaries,
    分析.datas,
    [],
    name="pixelprobe",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
