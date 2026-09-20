# -*- mode: python ; coding: utf-8 -*-
"""PixelProbe 独立 CLI 的 PyInstaller 构建定义。"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_all, copy_metadata

完整依赖 = [collect_all(name) for name in ("mcp", "cv2", "zarr", "c2pa", "psutil")]


项目根目录 = Path(SPECPATH).parent

分析 = Analysis(
    [str(项目根目录 / "src" / "pixelprobe" / "__main__.py")],
    pathex=[str(项目根目录 / "src")],
    binaries=[item for _, binaries, _ in 完整依赖 for item in binaries],
    # numcodecs 通过 importlib.metadata 读取版本，缺少元数据会使整个 Zarr 导入失败。
    datas=[item for datas, _, _ in 完整依赖 for item in datas] + copy_metadata("numcodecs"),
    # Typer 运行时直接依赖 Click。部分平台上的 PyInstaller 依赖分析会漏掉
    # Click 子模块，因此在发布包中显式收集，避免可执行文件启动失败。
    hiddenimports=["click", *collect_submodules("click"),
                   *[item for _, _, imports in 完整依赖 for item in imports]],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
