"""一次解码、磁盘后备、可供多个表示共享的 RGB FrameStore。"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

from pixelprobe.core.image_reader import ImageReader, NativeImageMetadata
from pixelprobe.core.media_reader import detect_media_type
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.domain.errors import MediaChangedDuringAnalysisError
from pixelprobe.domain.errors import MaterializationLimitExceededError
from pixelprobe.engine.errors import ResourcePlanUnsatisfiableError
from pixelprobe.domain.tensor import StorageKind
from pixelprobe.engine.execution import LocalExecutionContext
from pixelprobe.models.errors import InvalidRangeError


@dataclass(slots=True, frozen=True)
class FramePacketMetadata:
    """FrameStore 保存的逐展示帧解码元数据，不重复持有像素数组。"""

    presentation_index: int
    decode_index: int | None
    pts: int | None
    dts: int | None
    time_base: Fraction | None
    source_timestamp_seconds: float | None
    timeline_time_seconds: float
    duration_pts: int | None
    duration_seconds: float | None
    key_frame: bool | None
    stored_pixel_format: str | None
    decoded_pixel_format: str
    color_metadata: dict[str, object]
    sample_semantics: str
    flags: tuple[str, ...]


class RawFrameArrayHandle:
    __slots__ = ("_array", "_path", "_shape", "_dtype", "_chunk_shape", "_closed")

    def __init__(
        self,
        path: Path,
        shape: tuple[int, ...],
        *,
        dtype: np.dtype | type[np.generic] = np.uint8,
        chunk_shape: tuple[int, ...] | None = None,
    ) -> None:
        self._path = Path(path).resolve(strict=True)
        self._shape = shape
        self._dtype = np.dtype(dtype)
        self._chunk_shape = chunk_shape
        self._array = np.memmap(
            self._path, mode="r", dtype=self._dtype, shape=shape,
        )
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("FrameStore 已关闭")

    @property
    def shape(self) -> tuple[int, ...]:
        self._ensure_open()
        return self._shape

    @property
    def dtype(self) -> str:
        return str(self._dtype)

    @property
    def storage_kind(self) -> StorageKind:
        return StorageKind.MEMMAP

    @property
    def chunk_shape(self) -> tuple[int, ...] | None:
        return self._chunk_shape

    def read(self, selection: tuple[slice | int, ...]) -> np.ndarray:
        self._ensure_open()
        return np.asarray(self._array[selection]).copy()

    def materialize(self, *, max_bytes: int | None = None) -> np.ndarray:
        self._ensure_open()
        size = int(self._array.nbytes)
        if max_bytes is not None and size > max_bytes:
            raise ValueError(f"FrameStore 需要 {size} 字节，超过限制 {max_bytes}")
        return np.asarray(self._array).copy()

    def close(self) -> None:
        if self._closed:
            return
        mmap = getattr(self._array, "_mmap", None)
        if mmap is not None:
            mmap.close()
        self._closed = True


class SharedFrameStore:
    def __init__(self, path: Path, context: LocalExecutionContext) -> None:
        self.path = Path(path).resolve(strict=True)
        self.context = context
        self.width = 0
        self.height = 0
        self.fps: float | None = None
        self.times: tuple[float, ...] = ()
        self.frame_metadata: tuple[FramePacketMetadata, ...] = ()
        self.frames: RawFrameArrayHandle | None = None
        # 仅图片可用：保留 Pillow 原生样本（含 Alpha、位深、调色板索引等），
        # 不与现有 RGB8 计算帧混用。
        self.native_image: RawFrameArrayHandle | None = None
        self.native_image_metadata: NativeImageMetadata | None = None
        self.decode_passes = 0
        self._raw_path = context.temporary_root / f"frames-{uuid.uuid4().hex}.rgb24"
        self._native_path = context.temporary_root / f"native-image-{uuid.uuid4().hex}.bin"

    def _check_frame_budget(
        self,
        frame: np.ndarray,
        next_count: int,
        *,
        additional_memory_bytes: int = 0,
        additional_temporary_bytes: int = 0,
    ) -> None:
        required_memory = frame.nbytes + additional_memory_bytes
        if required_memory > self.context.resources.max_memory_bytes:
            raise MaterializationLimitExceededError(
                f"单帧完整分辨率需要 {required_memory} 字节，超过内存限制 "
                f"{self.context.resources.max_memory_bytes} 字节"
            )
        temporary_limit = self.context.resources.max_temporary_bytes
        required = next_count * frame.nbytes + additional_temporary_bytes
        if temporary_limit is not None and required > temporary_limit:
            raise ResourcePlanUnsatisfiableError(
                f"完整 FrameStore 需要至少 {required} 字节临时空间，"
                f"超过限制 {temporary_limit} 字节"
            )

    def decode(self) -> "SharedFrameStore":
        before = self.path.stat()
        times: list[float] = []
        metadata: list[FramePacketMetadata] = []
        count = 0
        with self._raw_path.open("xb") as output:
            if detect_media_type(self.path) == "image":
                with ImageReader() as reader:
                    reader.open(self.path)
                    info = reader.get_info()
                    self.width, self.height = info.width, info.height
                    self.fps = None
                    native = reader.get_native_frame()
                    native_metadata = reader.get_native_metadata()
                    frame = reader.get_engine_frame()
                    sample_semantics = reader.engine_sample_semantics()
                    conversion_flags = reader.engine_conversion_flags()
                if frame.shape != (self.height, self.width, 3):
                    raise ValueError("图片必须解码为完整分辨率 RGB")
                self._check_frame_budget(
                    frame,
                    1,
                    additional_memory_bytes=native.nbytes,
                    additional_temporary_bytes=native.nbytes,
                )
                self.decode_passes += 1
                output.write(memoryview(frame).cast("B"))
                with self._native_path.open("xb") as native_output:
                    native_output.write(memoryview(native).cast("B"))
                    native_output.flush()
                    os.fsync(native_output.fileno())
                times.append(0.0)
                metadata.append(FramePacketMetadata(
                    presentation_index=0,
                    decode_index=None,
                    pts=None,
                    dts=None,
                    time_base=None,
                    source_timestamp_seconds=None,
                    timeline_time_seconds=0.0,
                    duration_pts=None,
                    duration_seconds=None,
                    key_frame=None,
                    stored_pixel_format=info.color_mode,
                    decoded_pixel_format="rgb24",
                    color_metadata={
                        "source_mode": native_metadata.mode,
                        "source_format": native_metadata.source_format,
                        "native_dtype": native_metadata.dtype,
                        "native_shape": list(native_metadata.shape),
                        "native_bands": list(native_metadata.bands),
                        "native_bits_per_sample": native_metadata.bits_per_sample,
                        "has_alpha": native_metadata.has_alpha,
                        "alpha_representation": native_metadata.alpha_representation,
                        "native_sample_semantics": native_metadata.sample_semantics,
                    },
                    sample_semantics=sample_semantics,
                    flags=(
                        "IMAGE_SINGLE_FRAME",
                        "PTS_NOT_APPLICABLE",
                        *conversion_flags,
                    ),
                ))
                self.native_image_metadata = native_metadata
                count = 1
            else:
                with VideoReader() as reader:
                    reader.open(self.path)
                    info = reader.get_info()
                    self.width, self.height = info.width, info.height
                    self.fps = info.fps
                    self.decode_passes += 1
                    for packet in reader.iter_frame_packets(0, None):
                        self.context.ensure_active()
                        frame = np.ascontiguousarray(packet.data, dtype=np.uint8)
                        if frame.shape != (self.height, self.width, 3):
                            raise ValueError("解码期间帧尺寸或像素格式发生变化")
                        self._check_frame_budget(frame, count + 1)
                        output.write(memoryview(frame).cast("B"))
                        times.append(packet.timeline_time_seconds)
                        metadata.append(FramePacketMetadata(
                            presentation_index=packet.presentation_index,
                            decode_index=packet.decode_index,
                            pts=packet.pts,
                            dts=packet.dts,
                            time_base=packet.time_base,
                            source_timestamp_seconds=packet.source_timestamp_seconds,
                            timeline_time_seconds=packet.timeline_time_seconds,
                            duration_pts=packet.duration_pts,
                            duration_seconds=packet.duration_seconds,
                            key_frame=packet.key_frame,
                            stored_pixel_format=packet.stored_pixel_format,
                            decoded_pixel_format=packet.decoded_pixel_format,
                            color_metadata=dict(packet.color_metadata),
                            sample_semantics=packet.sample_semantics,
                            flags=packet.flags,
                        ))
                        count += 1
            output.flush()
            os.fsync(output.fileno())
        after = self.path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise MediaChangedDuringAnalysisError("分析期间媒体文件发生变化")
        if count == 0:
            raise ValueError("媒体没有可解码帧")
        if len(metadata) != count:
            raise ValueError("FrameStore 元数据与帧数不一致")
        expected = count * self.height * self.width * 3
        if self._raw_path.stat().st_size != expected:
            raise ValueError("FrameStore 字节数与完整分辨率不一致")
        self.times = tuple(times)
        self.frame_metadata = tuple(metadata)
        self.frames = RawFrameArrayHandle(
            self._raw_path,
            (count, self.height, self.width, 3),
            chunk_shape=(1, self.height, self.width, 3),
        )
        if self.native_image_metadata is not None:
            expected_native_bytes = (
                int(np.prod(self.native_image_metadata.shape, dtype=np.int64))
                * np.dtype(self.native_image_metadata.dtype).itemsize
            )
            if self._native_path.stat().st_size != expected_native_bytes:
                raise ValueError("原生图片样本字节数与元数据不一致")
            self.native_image = RawFrameArrayHandle(
                self._native_path,
                self.native_image_metadata.shape,
                dtype=np.dtype(self.native_image_metadata.dtype),
            )
        return self

    def selection_indices(self, selection) -> tuple[int, ...]:
        count = len(self.times)
        if selection.mode == "all":
            values = tuple(range(0, count, selection.sample_every))
        elif selection.mode == "frame_interval":
            assert selection.requested_start_frame is not None
            assert selection.requested_end_frame_exclusive is not None
            if (
                selection.requested_start_frame >= count
                or selection.requested_end_frame_exclusive > count
            ):
                raise InvalidRangeError(
                    "请求帧范围 "
                    f"[{selection.requested_start_frame},"
                    f"{selection.requested_end_frame_exclusive}) 超出实际展示帧范围 "
                    f"[0,{count})"
                )
            values = tuple(range(
                selection.requested_start_frame,
                selection.requested_end_frame_exclusive,
                selection.sample_every,
            ))
        elif selection.mode == "indices":
            values = selection.requested_indices[::selection.sample_every]
        else:
            values = tuple(
                index for index, timestamp in enumerate(self.times)
                if selection.requested_start_seconds <= timestamp < selection.requested_end_seconds
            )[::selection.sample_every]
        if not values or values[-1] >= count:
            raise ValueError("时间选择超出实际展示帧范围")
        return values

    def close(self) -> None:
        if self.frames is not None:
            self.frames.close()
        if self.native_image is not None:
            self.native_image.close()
        self._raw_path.unlink(missing_ok=True)
        self._native_path.unlink(missing_ok=True)

    def __enter__(self) -> "SharedFrameStore":
        return self.decode()

    def __exit__(self, *exc_info: object) -> None:
        self.close()
