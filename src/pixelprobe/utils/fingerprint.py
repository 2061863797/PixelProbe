"""媒体文件身份的两级指纹。

- 完整 SHA-256：读取全部内容，用于正式结果包（Bundle）的溯源与证据。
- 快速指纹：只读取文件大小、修改时间、文件标识和头尾各 1 MiB，用于日常查询的
  缓存键和"分析期间文件是否改变"的复验。

快速指纹的局限：若文件中间内容被改写、而大小、修改时间和头尾内容都保持不变，
快速指纹无法察觉。因此凡是写入正式结果包的身份，一律使用完整 SHA-256。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

QUICK_FINGERPRINT_VERSION = "pixelprobe-quick-fingerprint-v1"
_EDGE_BYTES = 1024 * 1024


def quick_fingerprint(path: Path) -> str:
    """返回 64 位十六进制的快速指纹；与完整 SHA-256 不可互换比较。"""
    stat = os.stat(path)
    digest = hashlib.sha256()
    digest.update(QUICK_FINGERPRINT_VERSION.encode("ascii"))
    digest.update(
        f"\0{stat.st_size}\0{stat.st_mtime_ns}\0{stat.st_dev:x}:{stat.st_ino:x}\0".encode("ascii")
    )
    with open(path, "rb") as handle:
        digest.update(handle.read(_EDGE_BYTES))
        if stat.st_size > _EDGE_BYTES:
            handle.seek(max(_EDGE_BYTES, stat.st_size - _EDGE_BYTES))
            digest.update(handle.read(_EDGE_BYTES))
    return digest.hexdigest()


def source_identity(path: Path, *, full: bool) -> str:
    """按需返回完整 SHA-256（full=True）或快速指纹。

    调用方在一次执行中只使用一种，前后复验也必须用同一种，两种值之间不做比较。
    """
    if full:
        from pixelprobe.artifacts import sha256_file
        return sha256_file(path)
    return quick_fingerprint(path)
