"""不改变坐标语义的 Tensor 分块、halo 与有界 NPY Sink。"""

from __future__ import annotations

import itertools
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from pixelprobe.artifacts.array_io import NpyArrayHandle
from pixelprobe.domain.tensor import TensorField
from pixelprobe.engine.errors import ChunkMappingMismatchError
from pixelprobe.operators.base import HaloSpec, TensorChunk


def choose_chunk_shape(
    shape: tuple[int, ...], dtype: str, max_bytes: int,
) -> tuple[int, ...]:
    if max_bytes < np.dtype(dtype).itemsize:
        raise ValueError("内存预算小于单个元素")
    chunk = list(shape)
    while int(np.prod(chunk, dtype=np.int64)) * np.dtype(dtype).itemsize > max_bytes:
        axis = max(range(len(chunk)), key=chunk.__getitem__)
        if chunk[axis] == 1:
            raise ValueError("无法在内存预算内生成合法 chunk")
        chunk[axis] = max(1, (chunk[axis] + 1) // 2)
    return tuple(chunk)


def iter_tensor_chunks(
    tensor: TensorField,
    chunk_shape: tuple[int, ...],
    *,
    halos: tuple[HaloSpec, ...] | None = None,
) -> Iterator[TensorChunk]:
    shape = tensor.data.shape
    if len(chunk_shape) != len(shape) or any(size < 1 for size in chunk_shape):
        raise ValueError("chunk_shape 必须与 Tensor 同维且全部 >= 1")
    halos = halos or tuple(HaloSpec() for _ in shape)
    if len(halos) != len(shape):
        raise ValueError("halos 必须与 Tensor 同维")
    starts = [range(0, length, size) for length, size in zip(shape, chunk_shape)]
    for chunk_index, core_starts in enumerate(itertools.product(*starts)):
        core = tuple(
            slice(start, min(start + size, length))
            for start, size, length in zip(core_starts, chunk_shape, shape)
        )
        read = tuple(
            slice(max(0, part.start - halo.before), min(length, part.stop + halo.after))
            for part, halo, length in zip(core, halos, shape)
        )
        validity = tensor.validity.read(read) if tensor.validity is not None else None
        yield TensorChunk(
            tensor_id=tensor.tensor_id,
            data=tensor.data.read(read),
            read_selection=read,
            core_selection=core,
            axis_mappings=tensor.axis_mappings,
            validity=validity,
            chunk_index=np.unravel_index(
                chunk_index,
                tuple((length + size - 1) // size for length, size in zip(shape, chunk_shape)),
            ),
        )


def core_view(chunk: TensorChunk) -> np.ndarray:
    local = tuple(
        slice(core.start - read.start, core.stop - read.start)
        for core, read in zip(chunk.core_selection, chunk.read_selection)
    )
    return chunk.data[local]


class NpyArtifactSink:
    """将 core chunk 直接写入临时 NPY，峰值内存由 chunk 决定。"""

    def __init__(
        self,
        target: Path,
        shape: tuple[int, ...],
        dtype: str,
        *,
        expected_chunks: int,
        overwrite: bool = False,
    ) -> None:
        self.target = Path(target)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        if self.target.exists() and not overwrite:
            raise FileExistsError(self.target)
        self._temporary = self.target.with_name(
            f".{self.target.name}.{uuid.uuid4().hex}.tmp"
        )
        self._array = np.lib.format.open_memmap(
            self._temporary, mode="w+", dtype=np.dtype(dtype), shape=shape,
        )
        self._expected = expected_chunks
        self._written: set[tuple[tuple[int, int], ...]] = set()
        self._closed = False

    def write(self, chunk: TensorChunk) -> None:
        if self._closed:
            raise ValueError("ArtifactSink 已关闭")
        key = tuple((part.start, part.stop) for part in chunk.core_selection)
        if key in self._written:
            raise ChunkMappingMismatchError("core selection 重复写入")
        data = core_view(chunk)
        expected = tuple(part.stop - part.start for part in chunk.core_selection)
        if data.shape != expected:
            raise ChunkMappingMismatchError("core data shape 与 selection 不一致")
        self._array[chunk.core_selection] = data
        self._written.add(key)

    def finalize(self) -> NpyArrayHandle:
        if len(self._written) != self._expected:
            self.abort()
            raise ChunkMappingMismatchError(
                f"chunk 不完整：{len(self._written)}/{self._expected}"
            )
        self._array.flush()
        mmap = getattr(self._array, "_mmap", None)
        if mmap is not None:
            mmap.close()
        self._closed = True
        os.replace(self._temporary, self.target)
        return NpyArrayHandle(self.target)

    def abort(self) -> None:
        if not self._closed:
            mmap = getattr(self._array, "_mmap", None)
            if mmap is not None:
                mmap.close()
            self._closed = True
        self._temporary.unlink(missing_ok=True)
