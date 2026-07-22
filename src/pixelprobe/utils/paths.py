"""路径与安全写入工具。

统一使用 pathlib.Path。所有输出先写入同目录临时文件，成功后原子替换，
中断（如 Ctrl+C）时清理临时文件，避免留下半成品。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pixelprobe.models.errors import OutputWriteError


@contextmanager
def atomic_output(final_path: Path) -> Iterator[Path]:
    """产出一个临时路径供写入，写入成功后原子替换到 final_path。

    任何异常（包括 KeyboardInterrupt）都会清理临时文件后重新抛出；
    文件系统错误统一包装为 OutputWriteError（退出码 8）。
    """
    final_path = Path(final_path)
    tmp_path = final_path.with_name(f"{final_path.name}.tmp-{os.getpid()}")
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputWriteError(
            f"无法创建输出目录：{final_path.parent}（{exc}）"
        ) from exc
    try:
        yield tmp_path
        os.replace(tmp_path, final_path)
    except OutputWriteError:
        raise
    except OSError as exc:
        raise OutputWriteError(
            f"写入输出文件失败：{final_path}（{exc}）",
            hint="请检查磁盘空间与目录写权限",
        ) from exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def file_size(path: Path) -> int:
    """返回文件大小（字节）。"""
    return Path(path).stat().st_size
