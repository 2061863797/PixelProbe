"""帧范围解析。

规则：
- 起始帧和结束帧都包含（闭区间）；
- 帧范围（--start-frame/--end-frame）与秒数范围（--start/--end）不能混用；
- 秒数按"时间戳不大于目标时间的最后一帧"换算为帧号。
"""

from __future__ import annotations

from dataclasses import dataclass

from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import FrameOutOfRangeError, InvalidRangeError
from pixelprobe.utils.validation import ensure_sample_every


@dataclass(frozen=True)
class FrameRange:
    """解析后的闭区间帧范围。"""

    start: int
    end: int
    sample_every: int

    @property
    def count(self) -> int:
        """采样后的帧数。"""
        return (self.end - self.start) // self.sample_every + 1


def resolve_range(
    reader: VideoReader,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
) -> FrameRange:
    """把 CLI 的范围参数解析为帧号闭区间。"""
    ensure_sample_every(sample_every)
    uses_frames = start_frame is not None or end_frame is not None
    uses_seconds = start is not None or end is not None
    if uses_frames and uses_seconds:
        raise InvalidRangeError(
            "帧范围（--start-frame/--end-frame）与时间范围（--start/--end）不能混用"
        )

    if uses_seconds:
        s, e, count = reader.frame_range_for_times(start, end)
        last = count - 1
    else:
        count, estimated = reader.frame_count()
        if count is None or estimated:
            # 元数据不可信时解码展示帧，构建精确 PTS 索引和帧数。
            index = reader.build_pts_index()
            if index:
                count, estimated = len(index), False
        if count is None or count < 1:
            raise FrameOutOfRangeError(
                "无法确定视频总帧数，无法解析帧范围",
                hint="请检查视频文件是否完整",
            )
        last = count - 1
        s = start_frame if start_frame is not None else 0
        e = end_frame if end_frame is not None else last

    if s < 0 or s > last:
        raise FrameOutOfRangeError(
            f"起始帧 {s} 超出有效范围 0～{last}"
        )
    if e < 0 or e > last:
        raise FrameOutOfRangeError(
            f"结束帧 {e} 超出有效范围 0～{last}"
        )
    if s > e:
        raise InvalidRangeError(
            f"起始帧 {s} 不能大于结束帧 {e}",
            hint="帧范围为闭区间，起始帧和结束帧都包含在内",
        )
    return FrameRange(start=s, end=min(e, last), sample_every=sample_every)
