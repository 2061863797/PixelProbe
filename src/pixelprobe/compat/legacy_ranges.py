"""旧闭区间帧范围与新半开区间选择模型之间的显式适配。"""

from __future__ import annotations

from collections.abc import Sequence

from pixelprobe.core.frame_selector import FrameRange
from pixelprobe.domain.time import TemporalSelection


def legacy_frame_range_to_selection(
    frame_range: FrameRange,
    *,
    timestamps_seconds: Sequence[float] | None = None,
    mapping_id: str | None = None,
) -> TemporalSelection:
    """把旧 `[start,end]` 转换为新 `[start,end+1)`，不改变采样帧。"""
    indices = tuple(range(frame_range.start, frame_range.end + 1, frame_range.sample_every))
    timestamps = tuple(timestamps_seconds or ())
    if timestamps and len(timestamps) != len(indices):
        raise ValueError("timestamps_seconds 数量必须与旧范围实际采样帧数一致")
    return TemporalSelection(
        mode="frame_interval",
        requested_start_frame=frame_range.start,
        requested_end_frame_exclusive=frame_range.end + 1,
        sample_every=frame_range.sample_every,
        resolved_presentation_indices=indices if timestamps else (),
        resolved_timestamps_seconds=timestamps,
        mapping_id=mapping_id,
    )


def selection_to_legacy_frame_range(selection: TemporalSelection) -> FrameRange:
    """把帧半开区间适配回旧闭区间；其他选择模式必须先解析。"""
    if selection.mode != "frame_interval":
        raise ValueError("只有 frame_interval 可以直接转换为旧 FrameRange")
    assert selection.requested_start_frame is not None
    assert selection.requested_end_frame_exclusive is not None
    return FrameRange(
        start=selection.requested_start_frame,
        end=selection.requested_end_frame_exclusive - 1,
        sample_every=selection.sample_every,
    )
