"""可选的 Zarr v3 分块数组访问。"""

from __future__ import annotations

import importlib
import os
import shutil
import time
import uuid
from pathlib import Path

import numpy as np

from pixelprobe.artifacts.array_io import _validate_selection, iter_chunk_selections
from pixelprobe.artifacts.errors import ZarrDependencyMissingError
from pixelprobe.domain.errors import MaterializationLimitExceededError
from pixelprobe.domain.tensor import ArrayHandle, MemoryArrayHandle, StorageKind
from pixelprobe.utils.resource_budget import current_budget, moved, removed, check_active, require_memory


_WINDOWS = os.name == "nt"


def _budget_store(path):
    budget = current_budget()
    if budget is None:
        return str(path)
    from zarr.storage import LocalStore

    class BudgetStore(LocalStore):
        async def set(self, key, value):
            budget.reserve(Path(path) / key, len(value))
            return await super().set(key, value)

        async def set_if_not_exists(self, key, value):
            budget.reserve(Path(path) / key, len(value))
            return await super().set_if_not_exists(key, value)

    return BudgetStore(path)


def _commit_zarr_directory(
    temporary: Path,
    target: Path,
    backup: Path,
    *,
    overwrite: bool,
) -> None:
    """提交完整 Zarr 目录；不覆盖时绝不主动替换现有目标。"""
    if not overwrite:
        if os.path.lexists(target):
            raise FileExistsError(f"输出目录已存在：{target}")
        try:
            _move_zarr_directory(temporary, target, replace=False)
            moved(temporary, target)
        except OSError as exc:
            if os.path.lexists(target):
                raise FileExistsError(f"输出目录已存在：{target}") from exc
            raise
        return
    if os.path.lexists(target):
        _move_zarr_directory(target, backup, replace=True)
    try:
        _move_zarr_directory(temporary, target, replace=True)
        moved(temporary, target)
    except Exception:
        if backup.exists() and not os.path.lexists(target):
            _move_zarr_directory(backup, target, replace=True)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _zarr():
    try:
        module = importlib.import_module("zarr")
    except ImportError as exc:
        raise ZarrDependencyMissingError(
            "Zarr 存储需要安装 pixelprobe[storage]"
        ) from exc
    if int(module.__version__.split(".", 1)[0]) != 3:
        raise ZarrDependencyMissingError("PixelProbe 只支持 Zarr Python 3.x")
    return module


def _close_zarr_store(array) -> None:
    """关闭 Zarr store，确保 Windows 可以移动或删除对应目录。"""
    close = getattr(getattr(array, "store", None), "close", None)
    if callable(close):
        close()


def _move_zarr_directory(source: Path, target: Path, *, replace: bool) -> None:
    """重命名完整目录；Windows 短暂占用时最多等待两秒。"""
    operation = os.replace if replace else os.rename
    deadline = time.monotonic() + 2.0
    while True:
        try:
            operation(source, target)
            return
        except PermissionError:
            if not _WINDOWS or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


class ZarrArrayHandle:
    __slots__ = ("_array", "_path")

    def __init__(self, path: Path) -> None:
        self._path = Path(path).resolve(strict=True)
        self._array = _zarr().open_array(store=str(self._path), mode="r")
        if np.dtype(self._array.dtype).hasobject:
            raise ValueError("不允许读取 object Zarr")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(self._array.shape)

    @property
    def dtype(self) -> str:
        return str(np.dtype(self._array.dtype))

    @property
    def storage_kind(self) -> StorageKind:
        return StorageKind.ZARR

    @property
    def chunk_shape(self) -> tuple[int, ...]:
        return tuple(self._array.chunks)

    def read(self, selection: tuple[slice | int, ...]) -> np.ndarray:
        _validate_selection(selection, self.shape)
        return np.asarray(self._array[selection]).copy()

    def materialize(self, *, max_bytes: int | None = None) -> np.ndarray:
        size = int(np.prod(self.shape, dtype=np.int64)) * np.dtype(self.dtype).itemsize
        if max_bytes is not None and size > max_bytes:
            raise MaterializationLimitExceededError(
                f"数组需要 {size} 字节，超过限制 {max_bytes} 字节"
            )
        return np.asarray(self._array[:]).copy()

    def close(self) -> None:
        _close_zarr_store(self._array)


def save_zarr(
    array: np.ndarray,
    path: Path,
    *,
    chunk_shape: tuple[int, ...],
    overwrite: bool = False,
    target_dtype: str | None = None,
) -> ZarrArrayHandle:
    if not isinstance(array, np.ndarray):
        raise TypeError("array 必须是 NumPy 数组")
    if array.dtype.hasobject:
        raise ValueError("不允许保存 object Zarr")
    if len(chunk_shape) != array.ndim or any(size < 1 for size in chunk_shape):
        raise ValueError("chunk_shape 必须与数组同维且全部 >= 1")
    zarr = _zarr()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(target) and not overwrite:
        raise FileExistsError(f"输出目录已存在：{target}")
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    backup = target.parent / f".{target.name}.{uuid.uuid4().hex}.backup"
    try:
        dtype = np.dtype(target_dtype or array.dtype)
        stored = zarr.create_array(
            store=_budget_store(temporary),
            shape=array.shape,
            chunks=chunk_shape,
            dtype=dtype,
            zarr_format=3,
            overwrite=False,
        )
        try:
            for selection in iter_chunk_selections(array.shape, chunk_shape):
                check_active()
                require_memory(int(np.prod(chunk_shape)) * dtype.itemsize * 4)
                stored[selection] = np.ascontiguousarray(array[selection])
        finally:
            _close_zarr_store(stored)
        checked = ZarrArrayHandle(temporary)
        try:
            if checked.shape != array.shape or np.dtype(checked.dtype) != dtype:
                raise ValueError("Zarr 写入后 shape/dtype 验证失败")
        finally:
            checked.close()
        _commit_zarr_directory(
            temporary, target, backup, overwrite=overwrite,
        )
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        removed(temporary)
    return ZarrArrayHandle(target)


def save_array_handle_zarr(
    source: ArrayHandle,
    path: Path,
    *,
    chunk_shape: tuple[int, ...],
    overwrite: bool = False,
    target_dtype: str | None = None,
) -> ZarrArrayHandle:
    if len(chunk_shape) != len(source.shape) or any(size < 1 for size in chunk_shape):
        raise ValueError("chunk_shape 必须与数组同维且全部 >= 1")
    zarr = _zarr()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(target) and not overwrite:
        raise FileExistsError(f"输出目录已存在：{target}")
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    backup = target.parent / f".{target.name}.{uuid.uuid4().hex}.backup"
    try:
        dtype = np.dtype(target_dtype or source.dtype)
        stored = zarr.create_array(
            store=_budget_store(temporary), shape=source.shape, chunks=chunk_shape,
            dtype=dtype, zarr_format=3, overwrite=False,
        )
        try:
            for selection in iter_chunk_selections(source.shape, chunk_shape):
                check_active()
                require_memory(int(np.prod(chunk_shape)) * dtype.itemsize * 4)
                stored[selection] = source.read(selection)
        finally:
            _close_zarr_store(stored)
        checked = ZarrArrayHandle(temporary)
        try:
            if checked.shape != source.shape or np.dtype(checked.dtype) != dtype:
                raise ValueError("Zarr 写入后 shape/dtype 验证失败")
        finally:
            checked.close()
        _commit_zarr_directory(
            temporary, target, backup, overwrite=overwrite,
        )
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        removed(temporary)
    return ZarrArrayHandle(target)
