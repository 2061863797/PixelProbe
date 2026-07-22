"""视频读取器（基于 PyAV）。

关键约定：
- 返回帧统一为 [height, width, 3] uint8，颜色顺序 RGB（解码后显式转换）；
- 帧索引从 0 开始，按展示顺序（presentation order）编号；
- 按帧号取帧：先 seek 到目标帧之前最近的关键帧，再向前解码丢弃，直到命中目标；
- 按时间取帧：选择时间戳不大于目标时间的最后一帧；
- frame_count 兜底链：PTS 索引（若已构建，精确）→ 容器元数据 →
  duration × fps 估算（info 标记 estimated）；
- Windows 非 ASCII 路径：直接传路径失败时回退为二进制文件对象打开；
- 可变帧率（VFR）与元数据缺失场景：demux 全部包构建 PTS 索引
  （只读包头不解码，速度远快于解码），帧号 = 排序后 PTS 的下标，
  寻址与帧计数均精确；索引在 VFR、容器缺少帧数元数据或 seek 需要
  精确定位时按需构建并缓存；无法建索引时退回从头顺序解码计数，
  保证帧索引内部一致，不静默返回错误结果。
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO

import av
import numpy as np
from av.video.reformatter import VideoReformatter

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
        # PTS 索引（presentation 顺序排序后的包 PTS 列表）；None 表示未构建，
        # False 表示尝试过但该文件无法构建（包缺少 PTS）
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

    # ---------- PTS 索引（精确 VFR 支持） ----------

    def build_pts_index(self) -> list[int] | None:
        """demux 全部包构建 PTS 索引（不解码，速度快）。

        返回按 presentation 顺序排序的 PTS 列表；帧号 i 对应第 i 个 PTS。
        包缺少 PTS/DTS 时返回 None（此后走顺序解码兜底）。
        结果缓存在实例上，只构建一次。
        """
        if self._pts_index is not None:
            return self._pts_index
        if self._pts_index_failed:
            return None
        self._open_container()
        assert self._container is not None and self._stream is not None
        pts_list: list[int] = []
        try:
            for packet in self._container.demux(self._stream):
                if packet.size == 0:
                    continue  # 尾部 flush 包
                pts = packet.pts if packet.pts is not None else packet.dts
                if pts is None:
                    self._pts_index_failed = True
                    self._open_container()
                    return None
                pts_list.append(pts)
        except av.FFmpegError as exc:
            raise DecodeError(
                f"构建 PTS 索引失败：{self._path}（{exc}）"
            ) from exc
        pts_list.sort()
        self._pts_index = pts_list
        self._open_container()  # 重置读取位置供后续解码
        return pts_list

    def frame_timestamps(self) -> list[float]:
        """返回每帧相对媒体起点的时间戳（秒）。

        只 demux 包头，不解码画面。缺少可靠 PTS 时明确失败，调用方可退回
        单帧解码模式，不能再用平均帧率伪造 VFR 帧映射。
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

        以下场景使用 PTS 索引二分查找（精确）：索引已构建、检测到 VFR、
        或容器缺少帧数元数据（此时元数据不可信，如 MKV）。
        仅在元数据可信的恒定帧率场景用 fps 公式（快速，不读全文件）。
        """
        assert self._stream is not None
        metadata_trusted = bool(self._stream.frames and self._stream.frames > 0)
        if self._pts_index is not None or self.is_vfr or not metadata_trusted:
            index = self.build_pts_index()
            if index:
                target_pts = index[0] + seconds / float(self.time_base)
                return max(bisect.bisect_right(index, target_pts) - 1, 0)
        fps = self.fps
        if not fps:
            raise DecodeError(f"无法获取帧率，无法按时间定位：{self._path}")
        return int(seconds * fps + 1e-6)

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

    def iter_frames(
        self,
        start_frame: int,
        end_frame: int | None,
        sample_every: int = 1,
    ) -> Iterator[tuple[int, float, np.ndarray]]:
        """迭代 [start_frame, end_frame] 闭区间内的帧（含两端）。

        产出 (帧号, 时间秒, RGB 数组)。end_frame 为 None 表示直到视频结束。
        寻址策略：索引已构建 / VFR / 元数据缺帧数时走 PTS 索引精确路径；
        元数据可信的恒定帧率视频走 fps 公式快速路径；
        两者失效时自动退回从头顺序解码，保证帧号准确。
        """
        assert self._container is not None and self._stream is not None
        metadata_trusted = bool(self._stream.frames and self._stream.frames > 0)
        if (
            self._pts_index is not None
            or self.is_vfr
            or not self.fps
            or not metadata_trusted
        ):
            yield from self._iter_indexed(start_frame, end_frame, sample_every)
            return

        fps = self.fps
        start_sec = self.start_time_seconds
        if start_frame > 0:
            target_time = start_sec + start_frame / fps
            try:
                offset = int(target_time / float(self.time_base))
                self._container.seek(
                    offset, stream=self._stream, backward=True, any_frame=False
                )
            except av.FFmpegError:
                yield from self._iter_sequential(
                    start_frame, end_frame, sample_every
                )
                return
        else:
            self._open_container()

        first = True
        try:
            for frame in self._container.decode(self._stream):
                t = frame.time
                if t is None:
                    # 无 PTS：放弃 seek 定位，退回顺序解码
                    yield from self._iter_sequential(
                        start_frame, end_frame, sample_every
                    )
                    return
                idx = int(round((t - start_sec) * fps))
                if first and idx > start_frame:
                    # seek 落点晚于目标帧，说明该策略对此文件不可靠
                    yield from self._iter_sequential(
                        start_frame, end_frame, sample_every
                    )
                    return
                first = False
                if idx < start_frame:
                    continue
                if end_frame is not None and idx > end_frame:
                    break
                if (idx - start_frame) % sample_every != 0:
                    continue
                yield (
                    idx,
                    round_seconds(t - start_sec),
                    self._frame_to_rgb_array(frame),
                )
        except av.FFmpegError as exc:
            raise DecodeError(
                f"视频解码失败：{self._path}（{exc}）"
            ) from exc

    def _iter_indexed(
        self,
        start_frame: int,
        end_frame: int | None,
        sample_every: int,
    ) -> Iterator[tuple[int, float, np.ndarray]]:
        """PTS 索引精确寻址路径（CFR/VFR 通用），无索引时顺序解码。"""
        index = self.build_pts_index()
        if not index:
            yield from self._iter_sequential(start_frame, end_frame, sample_every)
            return
        assert self._container is not None and self._stream is not None
        if start_frame >= len(index):
            return
        if start_frame > 0:
            try:
                self._container.seek(
                    index[start_frame], stream=self._stream,
                    backward=True, any_frame=False,
                )
            except av.FFmpegError:
                yield from self._iter_sequential(
                    start_frame, end_frame, sample_every
                )
                return
        first = True
        try:
            for frame in self._container.decode(self._stream):
                if frame.pts is None:
                    yield from self._iter_sequential(
                        start_frame, end_frame, sample_every
                    )
                    return
                # 帧号 = 该 PTS 在索引中的位置（presentation 顺序）
                pos = bisect.bisect_left(index, frame.pts)
                if pos >= len(index) or index[pos] != frame.pts:
                    # 解码出的 PTS 不在索引中，索引不可靠 → 顺序兜底
                    yield from self._iter_sequential(
                        start_frame, end_frame, sample_every
                    )
                    return
                if first and pos > start_frame:
                    yield from self._iter_sequential(
                        start_frame, end_frame, sample_every
                    )
                    return
                first = False
                if pos < start_frame:
                    continue
                if end_frame is not None and pos > end_frame:
                    break
                if (pos - start_frame) % sample_every != 0:
                    continue
                t = (frame.pts - index[0]) * float(self.time_base)
                yield pos, round_seconds(t), self._frame_to_rgb_array(frame)
        except av.FFmpegError as exc:
            raise DecodeError(
                f"视频解码失败：{self._path}（{exc}）"
            ) from exc

    def _iter_sequential(
        self,
        start_frame: int,
        end_frame: int | None,
        sample_every: int,
    ) -> Iterator[tuple[int, float, np.ndarray]]:
        """从头顺序解码，以解码顺序计数保证帧号内部一致。"""
        self._open_container()
        assert self._container is not None and self._stream is not None
        fps = self.fps
        timestamp_origin: float | None = None
        index = -1
        try:
            for frame in self._container.decode(self._stream):
                index += 1
                if frame.time is not None and timestamp_origin is None:
                    timestamp_origin = frame.time
                if index < start_frame:
                    continue
                if end_frame is not None and index > end_frame:
                    break
                if (index - start_frame) % sample_every != 0:
                    continue
                if frame.time is not None:
                    assert timestamp_origin is not None
                    t = frame.time - timestamp_origin
                elif fps:
                    t = index / fps
                else:
                    t = float(index)
                yield index, round_seconds(t), self._frame_to_rgb_array(frame)
        except av.FFmpegError as exc:
            raise DecodeError(
                f"视频解码失败：{self._path}（{exc}）"
            ) from exc
