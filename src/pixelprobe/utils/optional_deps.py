"""可选依赖门面：惰性导入，缺失时抛出带安装提示的业务错误。

光流等重功能依赖 opencv-python-headless（可选 extra [flow]）。
使用方在函数入口调用 require_cv2()，模块顶层禁止 import cv2，
以保证无 cv2 环境下 CLI/MCP 的注册与工具列表不受影响。
"""

from __future__ import annotations

from pixelprobe.models.errors import DependencyMissingError


def require_cv2():
    """返回 cv2 模块；未安装时抛 DependencyMissingError。"""
    try:
        import cv2
    except ImportError as exc:
        raise DependencyMissingError(
            "光流分析需要 OpenCV，但当前环境未安装",
            hint='pip install "pixelprobe[flow]"',
        ) from exc
    return cv2
