"""一次解码、磁盘后备、可供多个表示共享的 RGB FrameStore。"""

from __future__ import annotations

import os
import shutil
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
from pixelprobe.domain.time import TemporalSelection
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
    def __init__(
        self,
        path: Path,
        context: LocalExecutionContext,
        selections: tuple[TemporalSelection, ...] = (),
    ) -> None:
        self.path = Path(path).resolve(strict=True)
        self.context = context
        self.width = 0
        self.height = 0
        self.fps: float | None = None
        self.times: tuple[float, ...] = ()
        self.presentation_indices: tuple[int, ...] = ()
        self.frame_metadata: tuple[FramePacketMetadata, ...] = ()
        self.frames: RawFrameArrayHandle | None = None
        # 仅图片可用：保留 Pillow 原生样本（含 Alpha、位深、调色板索引等），
        # 不与现有 RGB8 计算帧混用。
        self.native_image: RawFrameArrayHandle | None = None
        self.native_image_metadata: NativeImageMetadata | None = None
        self.decode_passes = 0
        self.source_frames_decoded = 0
        self.frames_converted = 0
        self._selections = selections
        self._position_by_presentation: dict[int, int] = {}
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
        required_write = frame.nbytes + additional_temporary_bytes
        free_bytes = shutil.disk_usage(self.context.temporary_root).free
        if free_bytes < required_write:
            raise ResourcePlanUnsatisfiableError(
                f"临时目录剩余 {free_bytes} 字节，下一次原子写入至少需要 "
                f"{required_write} 字节"
            )
        self.context.reserve_temporary(self._raw_path, next_count * frame.nbytes)
        if additional_temporary_bytes:
            self.context.reserve_temporary(self._native_path, additional_temporary_bytes)

    def decode(self) -> "SharedFrameStore":
        try:
            return self._decode()
        except BaseException:
            self.close()
            raise

    def _decode(self) -> "SharedFrameStore":
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
                    self.context.budget.require_memory(self.width * self.height * 32)
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
                self.source_frames_decoded += 1
                self.frames_converted += 1
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
                    self.context.budget.require_memory(self.width * self.height * 32)
                    self.fps = info.fps
                    self.decode_passes += 1
                    select_frame, end_frame, stop_after = self._decode_plan()
                    for packet in reader.iter_frame_packets(
                        0, end_frame, select_frame=select_frame,
                        stop_after=stop_after,
                    ):
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
                    self.source_frames_decoded += reader.last_decoded_frame_count
                    self.frames_converted += reader.last_converted_frame_count
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
        self.presentation_indices = tuple(
            item.presentation_index for item in metadata
        )
        self._position_by_presentation = {
            presentation_index: position
            for position, presentation_index in enumerate(self.presentation_indices)
        }
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

    def _decode_plan(self):
        """合并同一来源的选择，返回单遍解码的选帧与停止条件。"""
        if not self._selections:
            return None, None, None
        if any(selection.mode == "all" and selection.sample_every == 1
               for selection in self._selections):
            return None, None, None

        explicit_indices: list[set[int]] = []
        frame_ranges: list[tuple[int, int, int]] = []
        all_steps: list[int] = []
        time_ranges: list[tuple[float, float]] = []
        for selection in self._selections:
            if selection.mode == "all":
                all_steps.append(selection.sample_every)
            elif selection.mode == "indices":
                explicit_indices.append(set(
                    selection.requested_indices[::selection.sample_every]
                ))
            elif selection.mode == "frame_interval":
                assert selection.requested_start_frame is not None
                assert selection.requested_end_frame_exclusive is not None
                frame_ranges.append((
                    selection.requested_start_frame,
                    selection.requested_end_frame_exclusive,
                    selection.sample_every,
                ))
            else:
                assert selection.requested_start_seconds is not None
                assert selection.requested_end_seconds is not None
                # 时间采样必须依据实际 PTS 排序后再应用 sample_every；先保留
                # 区间内全部帧，避免用平均帧率猜测展示帧号。
                time_ranges.append((
                    selection.requested_start_seconds,
                    selection.requested_end_seconds,
                ))

        def selected(index: int, timestamp: float) -> bool:
            decisions = [index % step == 0 for step in all_steps]
            decisions.extend(index in values for values in explicit_indices)
            decisions.extend(
                start <= index < end and (index - start) % step == 0
                for start, end, step in frame_ranges
            )
            decisions.extend(start <= timestamp < end for start, end in time_ranges)
            return any(decisions)

        if all_steps:
            return selected, None, None
        frame_ends = [max(values) for values in explicit_indices if values]
        frame_ends.extend(end - 1 for _, end, _ in frame_ranges)
        if not time_ranges:
            return selected, max(frame_ends), None
        # 未证明整个来源的时间戳单调时，后续帧可能回到请求区间。
        # 完整顺序扫描避免提前结束造成漏帧，仍只转换区间内的帧。
        self.context.events.append({
            "type": "decode_plan", "full_scan_required": True,
            "reason": "时间区间需要完整扫描以覆盖非单调时间戳",
        })
        return selected, None, None

    def selection_indices(self, selection) -> tuple[int, ...]:
        available = self.presentation_indices
        available_set = set(available)
        if selection.mode == "all":
            values = tuple(
                index for index in available
                if index % selection.sample_every == 0
            )
        elif selection.mode == "frame_interval":
            assert selection.requested_start_frame is not None
            assert selection.requested_end_frame_exclusive is not None
            if selection.requested_end_frame_exclusive > self.source_frames_decoded:
                raise InvalidRangeError("请求帧区间超出实际展示帧范围")
            values = tuple(range(
                selection.requested_start_frame,
                selection.requested_end_frame_exclusive,
                selection.sample_every,
            ))
        elif selection.mode == "indices":
            values = selection.requested_indices[::selection.sample_every]
        else:
            values = tuple(
                index for index, timestamp in zip(available, self.times)
                if selection.requested_start_seconds <= timestamp < selection.requested_end_seconds
            )[::selection.sample_every]
        if not values or any(index not in available_set for index in values):
            raise InvalidRangeError(
                "请求选择超出实际展示帧范围 "
                f"[0,{self.source_frames_decoded})"
            )
        return values

    def storage_position(self, presentation_index: int) -> int:
        try:
            return self._position_by_presentation[presentation_index]
        except KeyError as exc:
            raise InvalidRangeError(
                f"展示帧 {presentation_index} 未包含在共享解码计划中"
            ) from exc

    def timestamp(self, presentation_index: int) -> float:
        return self.times[self.storage_position(presentation_index)]

    def close(self) -> None:
        if self.frames is not None:
            self.frames.close()
        if self.native_image is not None:
            self.native_image.close()
        self._raw_path.unlink(missing_ok=True)
        self._native_path.unlink(missing_ok=True)
        self.context.release_temporary(self._raw_path)
        self.context.release_temporary(self._native_path)

    def __enter__(self) -> "SharedFrameStore":
        return self.decode()

    def __exit__(self, *exc_info: object) -> None:
        self.close()
