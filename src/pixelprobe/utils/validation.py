"""输入校验工具。"""

from __future__ import annotations

from pathlib import Path

from pixelprobe.models.errors import InvalidRangeError, MediaNotFoundError


def ensure_file_exists(path: Path) -> Path:
    """确认媒体文件存在，否则抛 MediaNotFoundError（退出码 3）。"""
    path = Path(path)
    if not path.exists():
        raise MediaNotFoundError(
            f"文件不存在：{path}",
            hint="请检查路径拼写；含空格的路径需要加引号",
        )
    if not path.is_file():
        raise MediaNotFoundError(f"路径不是文件：{path}")
    return path


def ensure_sample_every(sample_every: int) -> int:
    """校验采样间隔。"""
    if sample_every < 1:
        raise InvalidRangeError(
            f"--sample-every {sample_every} 无效，必须 >= 1"
        )
    return sample_every


def ensure_scale(value: int, name: str) -> int:
    """校验放大倍数。"""
    if value < 1:
        raise InvalidRangeError(f"{name} {value} 无效，必须 >= 1")
    return value
