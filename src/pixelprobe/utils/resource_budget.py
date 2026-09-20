"""执行作用域的磁盘预算；跨模块及 Zarr 写入线程共享同一账本。"""
from __future__ import annotations

import io
import errno
import math
import os
import shutil
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import numpy as np

_current: ContextVar[ResourceBudget | None] = ContextVar("pixelprobe_budget", default=None)


class ResourceBudget:
    def __init__(self, memory: int, disk: int | None, check=None):
        self.memory = memory
        self.disk = disk
        self.check = check
        self.sizes: dict[Path, int] = {}
        self.peak_disk_bytes = 0
        self.bundle_verification_passes = 0
        self.bundle_verified_bytes = 0
        self._lock = threading.RLock()

    def active(self):
        if self.check is not None:
            self.check()

    def require_memory(self, size: int):
        self.active()
        if size > self.memory:
            from pixelprobe.domain.errors import MaterializationLimitExceededError
            raise MaterializationLimitExceededError(
                f"完整解码、数组副本和中间结果预计需要 {size} 字节，超过内存预算 {self.memory}"
            )

    def reserve(self, path: Path, size: int):
        self.active()
        if size < 0:
            raise ValueError("文件大小不能为负数")
        path = Path(path).resolve()
        with self._lock:
            previous = self.sizes.get(path, 0)
            size = max(size, previous)
            required = sum(self.sizes.values()) - previous + size
            from pixelprobe.engine.errors import ResourcePlanUnsatisfiableError
            if self.disk is not None and required > self.disk:
                raise ResourcePlanUnsatisfiableError(
                    f"执行文件合计需要 {required} 字节，超过空间预算 {self.disk}"
                )
            parent = path.parent
            while not parent.exists():
                parent = parent.parent
            existing = path.stat().st_size if path.is_file() else 0
            # 已预留但尚未落盘的空间同样不能重复分配给另一个文件。
            device = parent.stat().st_dev
            pending = 0
            for other, reserved in self.sizes.items():
                if other == path:
                    continue
                ancestor = other.parent
                while not ancestor.exists():
                    ancestor = ancestor.parent
                if ancestor.stat().st_dev == device:
                    actual = other.stat().st_size if other.is_file() else 0
                    pending += max(0, reserved - actual)
            additional = max(0, size - existing)
            if shutil.disk_usage(parent).free < additional + pending:
                raise ResourcePlanUnsatisfiableError("目标磁盘剩余空间不足，已包含其他未落盘预留")
            self.sizes[path] = size
            self.peak_disk_bytes = max(self.peak_disk_bytes, required)

    def moved(self, source: Path, target: Path):
        source, target = Path(source).resolve(), Path(target).resolve()
        with self._lock:
            for path in list(self.sizes):
                if path == source or source in path.parents:
                    size = self.sizes.pop(path)
                    self.sizes[target / path.relative_to(source)] = size

    def removed(self, root: Path):
        root = Path(root).resolve()
        with self._lock:
            for path in list(self.sizes):
                if (path == root or root in path.parents) and not path.exists():
                    self.sizes.pop(path)


def current_budget():
    return _current.get()


@contextmanager
def budget_scope(budget: ResourceBudget):
    # 嵌套生成/证据调用必须沿用工作进程预算，不能重置额度。
    active = current_budget()
    token = _current.set(active if active is not None else budget)
    try:
        yield current_budget()
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            from pixelprobe.engine.errors import ResourcePlanUnsatisfiableError
            raise ResourcePlanUnsatisfiableError("磁盘写满或文件系统配额不足，结果未提交") from exc
        raise
    finally:
        _current.reset(token)


def reserve_write(path: Path, size: int):
    budget = current_budget()
    if budget is not None:
        budget.reserve(path, size)


def moved(source: Path, target: Path):
    budget = current_budget()
    if budget is not None:
        budget.moved(source, target)


def removed(path: Path):
    budget = current_budget()
    if budget is not None:
        budget.removed(path)


def check_active():
    budget = current_budget()
    if budget is not None:
        budget.active()


def require_memory(size: int):
    budget = current_budget()
    if budget is not None:
        budget.require_memory(size)


def npy_size(shape, dtype):
    dtype = np.dtype(dtype)
    header = io.BytesIO()
    np.lib.format.write_array_header_2_0(header, {
        "descr": np.lib.format.dtype_to_descr(dtype),
        "fortran_order": False, "shape": tuple(shape),
    })
    return math.prod(shape) * dtype.itemsize + header.tell()


def write_bytes(path: Path, data: bytes):
    reserve_write(path, len(data))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


class BudgetFile:
    """PNG 编码每次写入前按准确字节数检查，避免保守压缩比假设。"""
    def __init__(self, file, path):
        self.file, self.path = file, path

    def write(self, data):
        reserve_write(self.path, max(self.file.tell() + len(data),
                                    self.path.stat().st_size))
        return self.file.write(data)

    def __getattr__(self, name):
        # 禁止 Pillow 经文件描述符绕过 write。
        if name == "fileno":
            raise AttributeError(name)
        return getattr(self.file, name)
