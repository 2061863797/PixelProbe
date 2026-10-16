"""一次顺序解码生成媒体索引的四层数据。

L0 元数据：MediaInfo。
L1 每帧体检表（每个解码帧一行）：
    frame、time_seconds                     展示帧号与从 0 开始的时间
    mean_r/mean_g/mean_b                   原始分辨率逐通道均值
    luma_mean                              按 core.color 权重由通道均值线性换算
    min、max、std                          原始分辨率三通道合计的最小/最大/标准差
    diff_prev                              与上一帧逐通道绝对差的均值（0～255），首帧为 NaN；
                                           与 changes 整帧模式、scan 的变化得分是同一表达式
    sharpness                              长边缩到 256 像素（面积平均）的亮度做 4 邻域拉普拉斯，
                                           取方差；只用于相对比较，不是绝对清晰度
    dhash                                  9×8 亮度差分哈希（64 位），相似帧检索用
    blockiness                             原始分辨率整数亮度在 8 像素块边界处的平均相邻差 ÷ 块内部的平均相邻差
                                           （水平方向每 4 行、垂直方向每 4 列抽一条，两向取平均）；约 1 表示没有块效应，明显大于 1 表示
                                           块边界突出（压缩方块）；块内部完全平坦或画面小于 16 像素时为 NaN
L2 分段：由 diff_prev 按 segment_events 自动阈值得到的变化事件及其间的平稳段。
L3 缩略立方体：长边 64 像素（面积平均）的缩略帧；总量超出上限时按 thumb_stride 隔帧保留。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isnan
from pathlib import Path

import numpy as np
from PIL import Image

from pixelprobe.core.change_detector import ChangeRecord, segment_events
from pixelprobe.core.color import luminance_array
from pixelprobe.core.media_reader import detect_media_type
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import DecodeError, UnsupportedMediaError
from pixelprobe.models.media_info import MediaInfo

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], None]

SHARPNESS_LONG_SIDE = 256
THUMB_LONG_SIDE = 64
DEFAULT_THUMB_BUDGET_BYTES = 128 * 1024 * 1024
_LUMA_WEIGHTS = luminance_array(np.eye(3, dtype=np.uint8) * 1)  # 每个通道的亮度权重

FRAME_FIELDS = (
    "frame", "time_seconds", "mean_r", "mean_g", "mean_b", "luma_mean",
    "min", "max", "std", "diff_prev", "sharpness", "dhash", "blockiness",
)
BLOCK_SIZE = 8


@dataclass
class BuiltIndex:
    info: MediaInfo
    frames: dict[str, np.ndarray]
    segments: list[dict]
    segment_threshold: float | None
    thumbs: np.ndarray
    thumb_frames: np.ndarray
    thumb_stride: int


def _resized(image: Image.Image, long_side: int) -> Image.Image:
    width, height = image.size
    scale = long_side / max(width, height)
    if scale >= 1.0:
        return image
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(size, Image.Resampling.BOX)


def _sharpness(luma: np.ndarray) -> float:
    if luma.shape[0] < 3 or luma.shape[1] < 3:
        return 0.0
    center = luma[1:-1, 1:-1]
    laplacian = (
        luma[:-2, 1:-1] + luma[2:, 1:-1] + luma[1:-1, :-2] + luma[1:-1, 2:] - 4.0 * center
    )
    return float(laplacian.var())


def _boundary_ratio(gradient: np.ndarray, axis: int) -> float | None:
    """gradient 沿 axis 的第 i 个元素是像素 i 与 i+1 之差；i % 8 == 7 处跨越块边界。"""
    positions = np.arange(gradient.shape[axis])
    boundary = positions % BLOCK_SIZE == BLOCK_SIZE - 1
    if not boundary.any() or boundary.all():
        return None
    across = float(np.take(gradient, np.nonzero(boundary)[0], axis=axis).mean())
    inside = float(np.take(gradient, np.nonzero(~boundary)[0], axis=axis).mean())
    return None if inside == 0 else across / inside


def _luma16(arr: np.ndarray) -> np.ndarray:
    """整数近似 Rec.709 亮度（权重 54/183/19 ÷ 256，最大 65280 不溢出 uint16）。"""
    total = arr[..., 0].astype(np.uint16) * 54
    total += arr[..., 1].astype(np.uint16) * 183
    total += arr[..., 2].astype(np.uint16) * 19
    return (total >> 8).astype(np.int16)


def _blockiness(arr: np.ndarray) -> float:
    if arr.shape[0] < 2 * BLOCK_SIZE or arr.shape[1] < 2 * BLOCK_SIZE:
        return float("nan")
    # 块边界在每一行（列）上的位置相同，水平方向每 4 行、垂直方向每 4 列取一条即可
    rows, cols = _luma16(arr[::4]), _luma16(arr[:, ::4])
    ratios = [
        ratio for ratio in (
            _boundary_ratio(np.abs(np.diff(rows, axis=1)), 1),
            _boundary_ratio(np.abs(np.diff(cols, axis=0)), 0),
        ) if ratio is not None
    ]
    return float(np.mean(ratios)) if ratios else float("nan")


def _dhash(image: Image.Image) -> int:
    gray = np.asarray(image.convert("L").resize((9, 8), Image.Resampling.BOX), dtype=np.int16)
    bits = (gray[:, 1:] > gray[:, :-1]).ravel()
    return int(sum(1 << index for index, bit in enumerate(bits) if bit))


def frame_metrics(
    index: int, timestamp: float, arr: np.ndarray, previous: np.ndarray | None,
) -> tuple[dict[str, float | int], Image.Image]:
    """单帧的 L1 体检值（FRAME_FIELDS 各列）与供缩略图复用的缩小图。

    索引建立与无索引时的直接解码共用此函数，两条路径的数值逐值相同。
    """
    mean_rgb = arr.mean(axis=(0, 1))
    if previous is None:
        diff = float("nan")
    else:
        diff = float((np.maximum(arr, previous) - np.minimum(arr, previous)).mean())
    small = _resized(Image.fromarray(arr), SHARPNESS_LONG_SIDE)
    small_luma = luminance_array(np.asarray(small))
    row = {
        "frame": index, "time_seconds": timestamp,
        "mean_r": mean_rgb[0], "mean_g": mean_rgb[1], "mean_b": mean_rgb[2],
        "luma_mean": float(mean_rgb @ _LUMA_WEIGHTS),
        "min": int(arr.min()), "max": int(arr.max()), "std": float(arr.std()),
        "diff_prev": diff, "sharpness": _sharpness(small_luma), "dhash": _dhash(small),
        "blockiness": _blockiness(arr),
    }
    return row, small


def frame_columns(columns: dict[str, list]) -> dict[str, np.ndarray]:
    """把逐帧列表转换为与已提交索引相同 dtype 的列。"""
    frames = {
        "frame": np.asarray(columns["frame"], dtype=np.int64),
        "time_seconds": np.asarray(columns["time_seconds"], dtype=np.float64),
        "dhash": np.asarray(columns["dhash"], dtype=np.uint64),
        "min": np.asarray(columns["min"], dtype=np.uint8),
        "max": np.asarray(columns["max"], dtype=np.uint8),
    }
    for name in ("mean_r", "mean_g", "mean_b", "luma_mean", "std", "diff_prev", "sharpness",
                 "blockiness"):
        frames[name] = np.asarray(columns[name], dtype=np.float64)
    return frames


def _segments(frames: dict[str, np.ndarray]) -> tuple[list[dict], float | None]:
    """把逐帧差异切成"变化事件"与其间的"平稳段"，覆盖全部帧且互不重叠。"""
    indices = frames["frame"]
    if len(indices) < 2:
        only = int(indices[0])
        return [{"kind": "stable", "start_frame": only, "end_frame": only}], None
    records = [
        ChangeRecord(
            frame=int(indices[i]), previous_frame=int(indices[i - 1]),
            time_seconds=float(frames["time_seconds"][i]),
            score=round(float(frames["diff_prev"][i]), 4),
            normalized_score=round(float(frames["diff_prev"][i]) / 255.0, 6),
        )
        for i in range(1, len(indices))
    ]
    events, threshold = segment_events(records)
    segments: list[dict] = []
    cursor = int(indices[0])
    for event in events:
        if event.start_frame > cursor:
            segments.append({"kind": "stable", "start_frame": cursor, "end_frame": event.start_frame})
        segments.append({
            "kind": "change",
            "start_frame": event.start_frame,
            "end_frame": event.end_frame,
            "peak_frame": event.peak_frame,
            "peak_normalized": event.peak_normalized,
        })
        cursor = event.end_frame
    last = int(indices[-1])
    if cursor < last:
        segments.append({"kind": "stable", "start_frame": cursor, "end_frame": last})
    return segments, threshold


def build_index(
    path: Path,
    *,
    thumb_budget_bytes: int = DEFAULT_THUMB_BUDGET_BYTES,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> BuiltIndex:
    """顺序解码整段视频一次，返回四层索引数据（尚未写盘）。"""
    if detect_media_type(Path(path)) != "video":
        raise UnsupportedMediaError("只有视频需要建立索引；图片请直接分析")
    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        count, _ = reader.frame_count()
        expected = max(1, count or 1)
        thumb_bytes = None
        columns: dict[str, list] = {name: [] for name in FRAME_FIELDS}
        thumbs: list[np.ndarray] = []
        thumb_frames: list[int] = []
        thumb_stride = 1
        previous: np.ndarray | None = None
        done = 0
        for index, timestamp, arr in reader.iter_frames(0, None):
            if cancel_check is not None:
                cancel_check()
            row, small = frame_metrics(index, timestamp, arr, previous)
            for name, value in row.items():
                columns[name].append(value)
            if done % thumb_stride == 0:
                thumb = np.asarray(_resized(small, THUMB_LONG_SIDE), dtype=np.uint8)
                if thumb_bytes is None:
                    thumb_bytes = thumb.nbytes
                    capacity = max(1, thumb_budget_bytes // thumb_bytes)
                    thumb_stride = max(1, -(-expected // capacity))
                if done % thumb_stride == 0:
                    thumbs.append(thumb)
                    thumb_frames.append(index)
                if len(thumbs) * thumb_bytes > thumb_budget_bytes:
                    # 实际帧数超过预估：保留偶数位、步长加倍，总量回到上限内
                    thumbs, thumb_frames = thumbs[::2], thumb_frames[::2]
                    thumb_stride *= 2
            previous = arr
            done += 1
            if progress is not None:
                progress(done, max(done, expected))
        if done == 0:
            raise DecodeError("视频中没有解码出任何帧")
    frames = frame_columns(columns)
    segments, threshold = _segments(frames)
    return BuiltIndex(
        info=info,
        frames=frames,
        segments=segments,
        segment_threshold=None if threshold is None or isnan(threshold) else float(threshold),
        thumbs=np.stack(thumbs) if thumbs else np.zeros((0, 1, 1, 3), dtype=np.uint8),
        thumb_frames=np.asarray(thumb_frames, dtype=np.int64),
        thumb_stride=thumb_stride,
    )
