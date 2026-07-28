"""NPY 数组的精确保存与只读局部访问。"""

from __future__ import annotations

import os
import uuid
import itertools
from pathlib import Path

import numpy as np

from pixelprobe.domain.errors import (
    ArraySelectionOutOfRangeError,
    MaterializationLimitExceededError,
)
from pixelprobe.domain.tensor import ArrayHandle, StorageKind


def _validate_selection(
    selection: tuple[slice | int, ...],
    shape: tuple[int, ...],
) -> None:
    if len(selection) != len(shape):
        raise ArraySelectionOutOfRangeError(
            f"selection 维数 {len(selection)} 与数组维数 {len(shape)} 不一致"
        )
    for axis, (item, length) in enumerate(zip(selection, shape)):
        if isinstance(item, int):
            if item < 0 or item >= length:
                raise ArraySelectionOutOfRangeError(
                    f"第 {axis} 轴索引 {item} 超出 [0,{length})"
                )
            continue
        if not isinstance(item, slice):
            raise TypeError("selection 只支持 int 与 slice")
        start = 0 if item.start is None else item.start
        stop = length if item.stop is None else item.stop
        step = 1 if item.step is None else item.step
        if start < 0 or stop < 0 or start > length or stop > length or step <= 0:
            raise ArraySelectionOutOfRangeError(
                f"第 {axis} 轴切片必须位于 [0,{length}] 且 step > 0"
            )


class NpyArrayHandle:
    """使用 NumPy memmap 读取 NPY，不要求把完整数组载入内存。"""

    __slots__ = ("_array", "_path", "_closed")

    def __init__(self, path: Path) -> None:
        self._path = Path(path).resolve(strict=True)
        array = np.load(self._path, mmap_mode="r", allow_pickle=False)
        if not isinstance(array, np.ndarray):
            raise ValueError("NPY 文件没有包含数组")
        if array.dtype.hasobject:
            raise ValueError("不允许读取 object/pickle NPY")
        self._array = array
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError(f"NPY 句柄已关闭：{self._path}")

    def close(self) -> None:
        """释放 Windows 上会锁定目标文件的 memmap 句柄。"""
        if self._closed:
            return
        mmap = getattr(self._array, "_mmap", None)
        if mmap is not None:
            mmap.close()
        self._closed = True

    def __enter__(self) -> "NpyArrayHandle":
        self._ensure_open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


    @property
    def path(self) -> Path:
        return self._path

    @property
    def shape(self) -> tuple[int, ...]:
        self._ensure_open()
        return self._array.shape

    @property
    def dtype(self) -> str:
        self._ensure_open()
        return str(self._array.dtype)

    @property
    def storage_kind(self) -> StorageKind:
        return StorageKind.NPY

    @property
    def chunk_shape(self) -> tuple[int, ...] | None:
        return None

    def read(self, selection: tuple[slice | int, ...]) -> np.ndarray:
        self._ensure_open()
        _validate_selection(selection, self.shape)
        return np.array(self._array[selection], copy=True)

    def materialize(self, *, max_bytes: int | None = None) -> np.ndarray:
        self._ensure_open()
        if max_bytes is not None and self._array.nbytes > max_bytes:
            raise MaterializationLimitExceededError(
                f"数组需要 {self._array.nbytes} 字节，超过限制 {max_bytes} 字节"
            )
        return np.array(self._array, copy=True)


def save_npy(
    array: np.ndarray,
    path: Path,
    *,
    overwrite: bool = False,
) -> NpyArrayHandle:
    """先写同目录临时文件，再原子提交为 NPY。"""
    if not isinstance(array, np.ndarray):
        raise TypeError("array 必须是 NumPy 数组")
    if array.dtype.hasobject:
        raise ValueError("不允许保存 object/pickle NPY")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise FileExistsError(f"输出文件已存在：{target}")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            np.save(handle, np.ascontiguousarray(array), allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists() and not overwrite:
            raise FileExistsError(f"输出文件已存在：{target}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return NpyArrayHandle(target)


def iter_chunk_selections(
    shape: tuple[int, ...], chunk_shape: tuple[int, ...],
):
    if len(shape) != len(chunk_shape) or any(size < 1 for size in chunk_shape):
        raise ValueError("chunk_shape 必须与数组同维且全部 >= 1")
    starts = [range(0, length, size) for length, size in zip(shape, chunk_shape)]
    for offsets in itertools.product(*starts):
        yield tuple(
            slice(start, min(start + size, length))
            for start, size, length in zip(offsets, chunk_shape, shape)
        )


def choose_array_chunk_shape(
    shape: tuple[int, ...], dtype: str, max_bytes: int,
) -> tuple[int, ...]:
    chunk = list(shape)
    itemsize = np.dtype(dtype).itemsize
    while chunk and int(np.prod(chunk, dtype=np.int64)) * itemsize > max_bytes:
        axis = max(range(len(chunk)), key=chunk.__getitem__)
        chunk[axis] = max(1, (chunk[axis] + 1) // 2)
    return tuple(chunk)


def save_array_handle_npy(
    source: ArrayHandle,
    path: Path,
    *,
    chunk_bytes: int = 16 * 1024 * 1024,
    overwrite: bool = False,
    target_dtype: str | None = None,
) -> NpyArrayHandle:
    """按块复制任意 ArrayHandle，不整体物化源数组。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise FileExistsError(f"输出文件已存在：{target}")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    array = None
    try:
        dtype = np.dtype(target_dtype or source.dtype)
        array = np.lib.format.open_memmap(
            temporary, mode="w+", dtype=dtype, shape=source.shape,
        )
        chunk_shape = choose_array_chunk_shape(
            source.shape, str(dtype), chunk_bytes,
        )
        for selection in iter_chunk_selections(source.shape, chunk_shape):
            array[selection] = source.read(selection)
        array.flush()
        mmap = getattr(array, "_mmap", None)
        if mmap is not None:
            mmap.close()
        array = None
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if array is not None:
            mmap = getattr(array, "_mmap", None)
            if mmap is not None:
                mmap.close()
        temporary.unlink(missing_ok=True)
    return NpyArrayHandle(target)
