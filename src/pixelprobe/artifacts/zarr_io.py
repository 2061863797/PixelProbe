"""可选的 Zarr v3 分块数组访问。"""

from __future__ import annotations

import importlib
import os
import shutil
import uuid
from pathlib import Path

import numpy as np

from pixelprobe.artifacts.array_io import _validate_selection, iter_chunk_selections
from pixelprobe.artifacts.errors import ZarrDependencyMissingError
from pixelprobe.domain.errors import MaterializationLimitExceededError
from pixelprobe.domain.tensor import ArrayHandle, MemoryArrayHandle, StorageKind


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
    if target.exists() and not overwrite:
        raise FileExistsError(f"输出目录已存在：{target}")
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    backup = target.parent / f".{target.name}.{uuid.uuid4().hex}.backup"
    try:
        dtype = np.dtype(target_dtype or array.dtype)
        stored = zarr.create_array(
            store=str(temporary),
            shape=array.shape,
            chunks=chunk_shape,
            dtype=dtype,
            zarr_format=3,
            overwrite=False,
        )
        stored[:] = np.ascontiguousarray(array)
        checked = ZarrArrayHandle(temporary)
        if checked.shape != array.shape or np.dtype(checked.dtype) != dtype:
            raise ValueError("Zarr 写入后 shape/dtype 验证失败")
        if target.exists():
            os.replace(target, backup)
        os.replace(temporary, target)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return ZarrArrayHandle(target)


def save_array_handle_zarr(
    source: ArrayHandle,
    path: Path,
    *,
    chunk_shape: tuple[int, ...],
    overwrite: bool = False,
    target_dtype: str | None = None,
) -> ZarrArrayHandle:
    if isinstance(source, MemoryArrayHandle):
        return save_zarr(
            source.materialize(), path,
            chunk_shape=chunk_shape, overwrite=overwrite,
            target_dtype=target_dtype,
        )
    if len(chunk_shape) != len(source.shape) or any(size < 1 for size in chunk_shape):
        raise ValueError("chunk_shape 必须与数组同维且全部 >= 1")
    zarr = _zarr()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise FileExistsError(f"输出目录已存在：{target}")
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        dtype = np.dtype(target_dtype or source.dtype)
        stored = zarr.create_array(
            store=str(temporary), shape=source.shape, chunks=chunk_shape,
            dtype=dtype, zarr_format=3, overwrite=False,
        )
        for selection in iter_chunk_selections(source.shape, chunk_shape):
            stored[selection] = source.read(selection)
        checked = ZarrArrayHandle(temporary)
        if checked.shape != source.shape or np.dtype(checked.dtype) != dtype:
            raise ValueError("Zarr 写入后 shape/dtype 验证失败")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return ZarrArrayHandle(target)
