"""视频读取器（基于 PyAV）。

关键约定：
- 返回帧统一为 [height, width, 3] uint8，颜色顺序 RGB（解码后显式转换）；
- 帧索引从 0 开始，按展示顺序（presentation order）编号；
- 按帧号取帧：先 seek 到目标帧之前最近的关键帧，再向前解码丢弃，直到命中目标；
- 按时间取帧：选择时间戳不大于目标时间的最后一帧；
- frame_count 兜底链：PTS 索引（若已构建，精确）→ 容器元数据 →
  duration × fps 估算（info 标记 estimated）；
- Windows 非 ASCII 路径：直接传路径失败时回退为二进制文件对象打开；
- 可变帧率（VFR）与元数据缺失场景：解码展示帧构建 PTS 索引，
  帧号 = 展示顺序中的下标，
  寻址与帧计数均精确；索引在 VFR、容器缺少帧数元数据或 seek 需要
  精确定位时按需构建并缓存；无法建索引时退回从头顺序解码计数，
  保证帧索引内部一致，不静默返回错误结果。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO

import av
import numpy as np
from av.video.reformatter import VideoReformatter

from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.coordinates import TransformChain
from pixelprobe.domain.media import FramePacket
from pixelprobe.domain.references import ProvenanceRef
from pixelprobe.models.errors import (
    DecodeError,
    FrameOutOfRangeError,
    TimeOutOfRangeError,
    UnsupportedMediaError,
)
from pixelprobe.models.media_info import MediaInfo
from pixelprobe.utils.paths import file_size
from pixelprobe.utils.timecode import round_seconds
from pixelprobe.utils.validation import ensure_file_exists

# 判定 VFR 时 average_rate 与 base_rate 允许的相对偏差
_VFR_TOLERANCE = 1e-3


@dataclass(slots=True)
class _DecodedPresentationFrame:
    """一次顺序解码获得的展示帧元数据。

    ``data`` 仅为当前调用选中的帧生成，避免为跳过的帧额外做 RGB 转换。
    ``duration_*`` 在读到下一展示帧后回填；最后一帧没有可靠后继时为 ``None``。
    """

    presentation_index: int
    data: np.ndarray | None
    pts: int | None
    source_timestamp_seconds: float | None
    timeline_time_seconds: float
    duration_pts: int | None = None
    duration_seconds: float | None = None
    key_frame: bool | None = None
    flags: tuple[str, ...] = ()


class VideoReader:
    """单视频文件读取器，所有取帧接口共享一个容器句柄。"""

    def __init__(self) -> None:
        self._container: av.container.InputContainer | None = None
        self._stream: av.video.stream.VideoStream | None = None
        self._fileobj: BinaryIO | None = None
        self._path: Path | None = None
        # PyAV 的 VideoFrame.to_ndarray(format=...) 会为每帧新建颜色转换器。
        # 同一读取器内复用转换器，避免时间线/切片顺序解码时重复初始化。
        self._reformatter = VideoReformatter()
        # 仅在实际解码展示帧的 PTS 严格递增时缓存索引。重复、倒退或缺失
        # PTS 不能安全地映射为唯一帧号，必须走顺序解码并在 FramePacket 标记。
        self._pts_index: list[int] | None = None
        self._pts_index_failed: bool = False

    # ---------- 打开 / 关闭 ----------

    def open(self, path: Path) -> None:
        # PTS 索引只属于当前媒体。公开 open() 即使复用同一实例，也必须
        # 丢弃旧文件的索引状态；内部 _open_container() 重开当前文件时则保留。
        self._pts_index = None
        self._pts_index_failed = False
        self._path = ensure_file_exists(Path(path))
        self._open_container()

    def _open_container(self) -> None:
        """打开（或重新打开）容器。优先传路径字符串，失败时回退文件对象。"""
        assert self._path is not None
        self._close_container()
        try:
            self._container = av.open(str(self._path))
        except av.FFmpegError:
            # Windows 上 FFmpeg 对非 ASCII 路径偶有兼容问题，回退为文件对象
            try:
                self._fileobj = self._path.open("rb")
                self._container = av.open(self._fileobj)
            except av.FFmpegError as exc:
                raise UnsupportedMediaError(
                    f"无法打开媒体文件：{self._path}（{exc}）",
                    hint="支持 H.264/H.265 等常见编码的 MP4/MKV/AVI/MOV 等容器",
                ) from exc
        if not self._container.streams.video:
            raise UnsupportedMediaError(
                f"文件中没有视频流：{self._path}"
            )
        self._stream = self._container.streams.video[0]
        self._stream.thread_type = "AUTO"

    def _close_container(self) -> None:
        if self._container is not None:
            self._container.close()
            self._container = None
        if self._fileobj is not None:
            self._fileobj.close()
            self._fileobj = None
        self._stream = None

    def close(self) -> None:
        self._close_container()

    def __enter__(self) -> "VideoReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------- 基础属性 ----------

    @property
    def time_base(self) -> Fraction:
        assert self._stream is not None
        return self._stream.time_base or Fraction(1, 90000)

    @property
    def start_time_seconds(self) -> float:
        """视频流第一帧的时间戳（秒），缺失时为 0。"""
        assert self._stream is not None
        if self._stream.start_time is None:
            return 0.0
        return float(self._stream.start_time * self.time_base)

    @property
    def fps(self) -> float | None:
        assert self._stream is not None
        rate = self._stream.average_rate or self._stream.base_rate
        return float(rate) if rate else None

    @property
    def container_format(self) -> str | None:
        """返回解复用器识别的实际容器名，不使用文件扩展名。"""
        assert self._container is not None
        name = self._container.format.name
        return name.split(",", 1)[0].lower() if name else None

    @property
    def duration_seconds(self) -> float | None:
        assert self._stream is not None and self._container is not None
        if self._stream.duration is not None:
            return float(self._stream.duration * self.time_base)
        if self._container.duration is not None:
            duration = self._container.duration / av.time_base
            # 某些容器（例如带非零起始 PTS 的 MKV）把 format duration
            # 报告为时间轴终点；对外统一返回从媒体起点 0 开始的时长。
            if self._container.start_time is not None:
                start = self._container.start_time / av.time_base
                if start > 0 and duration > start:
                    duration -= start
            return duration
        return None

    @property
    def is_vfr(self) -> bool | None:
        """粗略 VFR 检测：average_rate 与 base_rate 明显不一致时判为 VFR。"""
        assert self._stream is not None
        avg, base = self._stream.average_rate, self._stream.base_rate
        if not avg or not base:
            return None
        return abs(float(avg) - float(base)) / float(base) > _VFR_TOLERANCE

    def frame_count(self) -> tuple[int | None, bool]:
        """返回 (总帧数, 是否为估算值)。

        兜底链：PTS 索引（已构建时精确）→ 容器元数据 → duration × fps 估算。
        均缺失时返回 (None, False)。
        """
        assert self._stream is not None
        if self._pts_index is not None:
            return len(self._pts_index), False
        if self._stream.frames and self._stream.frames > 0:
            return self._stream.frames, False
        duration, fps = self.duration_seconds, self.fps
        if duration is not None and fps:
            return int(round(duration * fps)), True
        return None, False

    # ---------- PTS 索引（可靠时的精确 VFR 支持） ----------

    def build_pts_index(self) -> list[int] | None:
        """为实际解码的展示帧建立严格递增的 PTS 索引。

        不能用 packet PTS 排序推导帧号：一个 packet 不保证只产出一帧，重复
        PTS 也无法用二分查找映射为唯一展示帧。只有每个已解码展示帧都有严格
        递增 PTS 时，返回的第 ``i`` 项才可靠对应展示帧 ``i``；否则返回
        ``None``，调用方必须顺序解码并保留异常标志。
        """
        if self._pts_index is not None:
            return self._pts_index
        if self._pts_index_failed:
            return None
        self._open_container()
        assert self._container is not None and self._stream is not None
        pts_list: list[int] = []
        previous: int | None = None
        try:
            for frame in self._container.decode(self._stream):
                pts = frame.pts
                if pts is None or (previous is not None and pts <= previous):
                    self._pts_index_failed = True
                    return None
                pts_list.append(pts)
                previous = pts
        except av.FFmpegError as exc:
            raise DecodeError(
                f"构建 PTS 索引失败：{self._path}（{exc}）"
            ) from exc
        finally:
            # 索引构建会消耗容器，后续读取必须从干净位置开始。
            self._open_container()
        if not pts_list:
            self._pts_index_failed = True
            return None
        self._pts_index = pts_list
        return self._pts_index

    def frame_timestamps(self) -> list[float]:
        """返回每帧相对媒体起点的时间戳（秒）。

        缺少唯一、严格递增的展示帧 PTS 时明确失败，不能用平均帧率伪造
        VFR 帧映射。
        """
        index = self.build_pts_index()
        if not index:
            raise DecodeError(f"无法获取可靠的逐帧时间戳：{self._path}")
        origin = index[0]
        time_base = float(self.time_base)
        return [round_seconds((pts - origin) * time_base) for pts in index]

    def get_info(self) -> MediaInfo:
        assert self._stream is not None and self._path is not None
        count, estimated = self.frame_count()
        ctx = self._stream.codec_context
        return MediaInfo(
            path=str(self._path),
            media_type="video",
            width=ctx.width,
            height=ctx.height,
            channels=3,
            fps=round(self.fps, 6) if self.fps else None,
            frame_count=count,
            frame_count_estimated=estimated,
            duration_seconds=(
                round_seconds(self.duration_seconds)
                if self.duration_seconds is not None
                else None
            ),
            codec=ctx.name,
            pixel_format=ctx.pix_fmt,
            is_vfr=self.is_vfr,
            time_base=str(self.time_base),
            file_size_bytes=file_size(self._path),
        )

    # ---------- 帧号 / 时间换算 ----------

    def frame_index_for_time(self, seconds: float) -> int:
        """按"时间戳不大于目标时间的最后一帧"规则换算帧号。

        仅可靠的展示帧 PTS 索引可用于此映射；索引不可靠时由
        :meth:`get_frame_by_time` 顺序扫描，避免用平均 FPS 伪造帧号。
        """
        index = self.build_pts_index()
        if not index:
            raise DecodeError(f"无法获取可靠的逐帧时间戳：{self._path}")
        target_pts = index[0] + seconds / float(self.time_base)
        return max(bisect.bisect_right(index, target_pts) - 1, 0)

    def frame_range_for_times(
        self,
        start_seconds: float | None,
        end_seconds: float | None,
    ) -> tuple[int, int, int]:
        """把时间闭区间映射为展示帧闭区间，并返回精确总帧数。

        PTS 严格递增时使用索引；缺失、重复或倒退时只顺序解码元数据，
        不用平均 FPS 伪造帧号，也不为扫描帧执行 RGB 数组转换。
        """
        if start_seconds is not None:
            self.validate_time(start_seconds)
        if end_seconds is not None:
            self.validate_time(end_seconds)
        index = self.build_pts_index()
        if index:
            origin = index[0]
            time_base = float(self.time_base)

            def indexed(seconds: float) -> int:
                target_pts = origin + seconds / time_base
                return max(bisect.bisect_right(index, target_pts) - 1, 0)

            start = indexed(start_seconds) if start_seconds is not None else 0
            end = indexed(end_seconds) if end_seconds is not None else len(index) - 1
            return start, end, len(index)

        start_index = 0 if start_seconds is None else None
        end_index: int | None = None
        last_index = -1
        for decoded in self._iter_decoded_presentation_frames(
            0, None, 1, include_data=False,
        ):
            last_index = decoded.presentation_index
            timestamp = decoded.timeline_time_seconds
            if start_seconds is not None and timestamp <= start_seconds + 1e-6:
                start_index = decoded.presentation_index
            if end_seconds is not None and timestamp <= end_seconds + 1e-6:
                end_index = decoded.presentation_index
        if last_index < 0:
            raise DecodeError(f"视频没有可解码帧：{self._path}")
        if start_index is None:
            raise TimeOutOfRangeError(f"时间 {start_seconds}s 之前没有任何帧")
        if end_seconds is None:
            end_index = last_index
        elif end_index is None:
            raise TimeOutOfRangeError(f"时间 {end_seconds}s 之前没有任何帧")
        return start_index, end_index, last_index + 1

    def validate_frame_index(self, index: int) -> None:
        """校验帧号范围，越界抛 FrameOutOfRangeError。"""
        count, estimated = self.frame_count()
        if index < 0 or (count is not None and index >= count):
            upper = f"{count - 1}" if count is not None else "?"
            note = "（估算值）" if estimated else ""
            raise FrameOutOfRangeError(
                f"帧号 {index} 超出有效范围 0～{upper}{note}",
                hint="帧号从 0 开始，最后一帧为 frame_count - 1",
            )

    def validate_time(self, seconds: float) -> None:
        """校验时间范围，越界抛 TimeOutOfRangeError。"""
        duration = self.duration_seconds
        if (
            not math.isfinite(seconds)
            or seconds < 0
            or (duration is not None and seconds > duration + 1e-6)
        ):
            upper = f"{round_seconds(duration)}" if duration is not None else "?"
            raise TimeOutOfRangeError(
                f"时间 {seconds}s 超出有效范围 0～{upper}s",
                hint="时间单位为秒，允许小数",
            )

    # ---------- 取帧 ----------

    def _frame_to_rgb_array(self, frame: av.VideoFrame) -> np.ndarray:
        """复用颜色转换上下文，把解码帧转换为 RGB 数组。"""
        rgb_frame = self._reformatter.reformat(frame, format="rgb24")
        if rgb_frame is None:
            raise DecodeError(f"视频帧颜色转换失败：{self._path}")
        return rgb_frame.to_ndarray()

    def get_frame_by_index(self, frame_index: int) -> tuple[float, np.ndarray]:
        """按帧号取帧，返回 (时间秒, RGB 数组)。"""
        self.validate_frame_index(frame_index)
        for idx, t, arr in self.iter_frames(frame_index, frame_index):
            if idx == frame_index:
                return t, arr
        count, _ = self.frame_count()
        raise FrameOutOfRangeError(
            f"帧号 {frame_index} 超出视频实际帧数"
            + (f"（元数据帧数 {count}）" if count is not None else ""),
            hint="该视频实际可解码帧数少于元数据声明，请改用更小的帧号",
        )

    def get_frame_by_time(self, seconds: float) -> tuple[int, float, np.ndarray]:
        """按时间取帧，返回 (帧号, 实际时间秒, RGB 数组)。

        规则：选择时间戳不大于目标时间的最后一帧。
        """
        self.validate_time(seconds)
        try:
            idx = self.frame_index_for_time(seconds)
        except DecodeError:
            return self._get_frame_by_time_sequential(seconds)
        count, _ = self.frame_count()
        if count is not None:
            idx = min(idx, count - 1)
        idx = max(idx, 0)
        t, arr = self.get_frame_by_index(idx)
        return idx, t, arr

    def _get_frame_by_time_sequential(
        self, seconds: float
    ) -> tuple[int, float, np.ndarray]:
        """兜底路径：顺序扫描，返回时间戳不大于目标的最后一帧。"""
        last: tuple[int, float, np.ndarray] | None = None
        for idx, t, arr in self._iter_sequential(0, None, 1):
            if t > seconds + 1e-6:
                break
            last = (idx, t, arr)
        if last is None:
            raise TimeOutOfRangeError(f"时间 {seconds}s 之前没有任何帧")
        return last

    def iter_frame_packets(
        self,
        start_frame: int,
        end_frame: int | None,
        sample_every: int = 1,
    ) -> Iterator[FramePacket]:
        """迭代规范展示帧包；输入范围沿用旧 CLI 的闭区间语义。

        帧号始终由实际解码产出顺序计数，不从 packet PTS 或 FPS 公式推导。
        PTS 异常会保留在 packet 的 ``flags`` 中，而不是改变帧号。
        """
        time_base = self.time_base
        assert self._stream is not None
        stored_format = self._stream.codec_context.pix_fmt
        provenance = ProvenanceRef(
            provenance_id=f"decode:pyav:{av.__version__}"
        )
        accuracy = AccuracyInfo(
            level=AccuracyLevel.DECODED,
            source=f"pyav:{av.__version__}",
            unit="code_value",
        )
        transform = TransformChain.identity("storage_pixels")

        for decoded in self._iter_decoded_presentation_frames(
            start_frame, end_frame, sample_every,
        ):
            assert decoded.data is not None
            yield FramePacket(
                data=decoded.data,
                presentation_index=decoded.presentation_index,
                decode_index=None,
                pts=decoded.pts,
                dts=None,
                time_base=time_base,
                source_timestamp_seconds=decoded.source_timestamp_seconds,
                timeline_time_seconds=decoded.timeline_time_seconds,
                duration_pts=decoded.duration_pts,
                duration_seconds=decoded.duration_seconds,
                key_frame=decoded.key_frame,
                stored_pixel_format=stored_format,
                decoded_pixel_format="rgb24",
                color_metadata={},
                transform_chain=transform,
                sample_semantics="decoded_sample",
                accuracy=accuracy,
                provenance=provenance,
                flags=decoded.flags,
            )

    def iter_frames(
        self,
        start_frame: int,
        end_frame: int | None,
        sample_every: int = 1,
    ) -> Iterator[tuple[int, float, np.ndarray]]:
        """兼容旧元组接口，内容由 FramePacket 无损适配。"""
        for packet in self.iter_frame_packets(start_frame, end_frame, sample_every):
            yield packet.presentation_index, packet.timeline_time_seconds, packet.data

    def _iter_frame_tuples(
        self,
        start_frame: int,
        end_frame: int | None,
        sample_every: int = 1,
    ) -> Iterator[tuple[int, float, np.ndarray]]:
        """兼容内部元组接口，帧号来自顺序展示解码。"""
        yield from self._iter_sequential(start_frame, end_frame, sample_every)

    def _iter_indexed(
        self,
        start_frame: int,
        end_frame: int | None,
        sample_every: int,
    ) -> Iterator[tuple[int, float, np.ndarray]]:
        """保留私有入口兼容性；不再从 PTS 二分反推展示帧号。"""
        yield from self._iter_sequential(start_frame, end_frame, sample_every)

    def _iter_sequential(
        self,
        start_frame: int,
        end_frame: int | None,
        sample_every: int,
    ) -> Iterator[tuple[int, float, np.ndarray]]:
        """从头顺序解码，以实际展示顺序计数保证帧号内部一致。"""
        for packet in self.iter_frame_packets(start_frame, end_frame, sample_every):
            yield packet.presentation_index, packet.timeline_time_seconds, packet.data

    def _iter_decoded_presentation_frames(
        self,
        start_frame: int,
        end_frame: int | None,
        sample_every: int,
        *,
        include_data: bool = True,
    ) -> Iterator[_DecodedPresentationFrame]:
        """顺序解码并保留展示帧元数据，不以包 PTS 或 FPS 推导帧号。"""
        if start_frame < 0:
            raise FrameOutOfRangeError("起始帧必须 >= 0")
        if end_frame is not None and end_frame < start_frame:
            raise FrameOutOfRangeError("结束帧不能小于起始帧")
        if sample_every < 1:
            raise ValueError("sample_every 必须 >= 1")
        self._open_container()
        assert self._container is not None and self._stream is not None
        fps = self.fps
        time_base = self.time_base
        timestamp_origin: float | None = None
        previous_pts: int | None = None
        pending: _DecodedPresentationFrame | None = None
        index = -1
        try:
            for frame in self._container.decode(self._stream):
                index += 1
                pts = frame.pts
                source_timestamp = (
                    float(pts * time_base) if pts is not None else None
                )
                decoded_timestamp = (
                    source_timestamp
                    if source_timestamp is not None else frame.time
                )
                if timestamp_origin is None and decoded_timestamp is not None:
                    timestamp_origin = float(decoded_timestamp)
                flags: list[str] = []
                if pts is None:
                    flags.append("PTS_MISSING")
                elif previous_pts is not None:
                    if pts == previous_pts:
                        flags.append("PTS_DUPLICATE")
                    elif pts < previous_pts:
                        flags.append("PTS_NON_MONOTONIC")
                if pts is not None:
                    previous_pts = pts
                if decoded_timestamp is not None and timestamp_origin is not None:
                    raw_timeline = float(decoded_timestamp) - timestamp_origin
                    if raw_timeline < 0:
                        flags.append("TIMELINE_NEGATIVE_NORMALIZED")
                    timeline = max(0.0, raw_timeline)
                elif fps:
                    timeline = index / fps
                    flags.append("TIMELINE_ESTIMATED_FROM_FPS")
                else:
                    timeline = float(index)
                    flags.append("TIMELINE_ESTIMATED_FROM_INDEX")
                selected = (
                    index >= start_frame
                    and (end_frame is None or index <= end_frame)
                    and (index - start_frame) % sample_every == 0
                )
                current = _DecodedPresentationFrame(
                    presentation_index=index,
                    data=(
                        self._frame_to_rgb_array(frame)
                        if selected and include_data else None
                    ),
                    pts=pts,
                    source_timestamp_seconds=source_timestamp,
                    timeline_time_seconds=round_seconds(timeline),
                    key_frame=bool(frame.key_frame),
                    flags=tuple(flags),
                )
                if pending is not None:
                    if pending.pts is not None and pts is not None:
                        duration_pts = pts - pending.pts
                        if duration_pts > 0:
                            pending.duration_pts = duration_pts
                            pending.duration_seconds = float(duration_pts * time_base)
                    if pending.data is not None or not include_data:
                        yield pending
                if end_frame is not None and index > end_frame:
                    return
                pending = current
            if pending is not None and (pending.data is not None or not include_data):
                yield pending
        except av.FFmpegError as exc:
            raise DecodeError(
                f"视频解码失败：{self._path}（{exc}）"
            ) from exc
