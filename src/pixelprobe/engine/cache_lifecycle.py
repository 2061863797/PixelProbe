"""缓存操作及数组句柄引用；维护时保守保护全部活动引用。"""
import uuid
import re
from contextlib import contextmanager
from pathlib import Path

from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.utils.file_lock import FileLease, require_plain


class CacheInUseError(InvalidRangeError):
    pass


@contextmanager
def gate(root: Path):
    try:
        lease = FileLease(root / ".cache.lock")
    except OSError as exc:
        raise CacheInUseError("CACHE_IN_USE：缓存正在维护，请稍后重试") from exc
    try:
        yield
    finally:
        lease.close()


def reference(root: Path) -> FileLease:
    with gate(root):
        refs = root / ".refs"
        require_plain(refs)
        refs.mkdir(exist_ok=True)
        return FileLease(refs / f"{uuid.uuid4().hex}.lock")


def require_idle(root: Path) -> None:
    refs = root / ".refs"
    require_plain(refs)
    if not refs.exists():
        return
    for index, path in enumerate(refs.iterdir()):
        if index >= 10000:
            raise CacheInUseError("CACHE_IN_USE：引用记录过多，无法安全确认空闲")
        if re.fullmatch(r"[0-9a-f]{32}\.lock", path.name) is None:
            raise CacheInUseError("CACHE_IN_USE：引用目录包含未知文件，保留等待检查")
        try:
            with FileLease(path):
                pass
        except OSError as exc:
            raise CacheInUseError("CACHE_IN_USE：仍有生成操作或数组句柄使用缓存") from exc
        # 登记与维护共用 gate，已关闭引用不会复用随机 ID。
        path.unlink()
