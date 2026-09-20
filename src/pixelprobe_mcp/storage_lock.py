"""本机存储租约；进程退出时由操作系统释放。"""
import os
from pathlib import Path

from pixelprobe_mcp.config import PathAccessError, _is_reparse_point


class StorageLease:
    def __init__(self, path: Path):
        if path.is_symlink() or _is_reparse_point(path):
            raise PathAccessError("存储租约不能是链接")
        self.handle = path.open("a+b")
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.handle.close()
            raise

    def close(self):
        self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
