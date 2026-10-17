"""可选依赖门面：惰性导入，缺失时抛出带安装提示的业务错误。

光流等重功能依赖 opencv-python-headless（可选 extra [flow]）。
使用方在函数入口调用 require_cv2()，模块顶层禁止 import cv2，
以保证无 cv2 环境下 CLI 的命令注册不受影响。
"""

from __future__ import annotations

from pixelprobe.models.errors import DependencyMissingError


def install_hint(extra: str) -> str:
    """缺可选依赖时的安装提示；同时覆盖 pip 安装与 uvx / 插件启动两种方式。"""
    return (
        f'pip 安装的执行 pip install "pixelprobe[{extra}]"；'
        f'用 uvx 或手动 MCP 配置启动的，在 --from 中加入该 extra，如 "pixelprobe[mcp,{extra}]"；'
        "插件用户更新插件到 2.1.1 或以上即可"
    )


def require_cv2():
    """返回 cv2 模块；未安装时抛 DependencyMissingError。"""
    try:
        import cv2
    except ImportError as exc:
        raise DependencyMissingError(
            "光流分析需要 OpenCV，但当前环境未安装",
            hint=install_hint("flow"),
        ) from exc
    return cv2
