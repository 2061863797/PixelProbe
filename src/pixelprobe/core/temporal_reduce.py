"""时间域合成：把整段视频折叠成一张逐像素统计图。

用途：让"只有跨帧统计才能看见"的内容显形——隐藏在噪声里的静态图案
（低时间方差区域）、慢变水印、坏点、局部闪烁、运动能量分布等。
除 median 外全部为流式聚合，内存 O(H*W)，视频只解码一遍。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import numpy as np

from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import DecodeError, InvalidRangeError
from pixelprobe.utils.coordinates import validate_rect

ProgressCallback = Callable[[int, int], None]

ReduceOp = Literal["mean", "median", "min", "max", "std", "diff"]

REDUCE_OPS: tuple[ReduceOp, ...] = ("mean", "median", "min", "max", "std", "diff")

# median 需要在内存中持有全部采样帧，缺省上限 1GB（含工作拷贝按 2 倍估算）
DEFAULT_MAX_MEDIAN_BYTES = 1_073_741_824
# smooth 邻域边长上限（过大只会把结构糊掉，且与 MCP/Web 层约束一致）
MAX_SMOOTH = 64


@dataclass
class TemporalReduceResult:
    """时间域合成结果。

    image 是经百分位对比度拉伸后的可视化统计图 [H,W,3] uint8；
    stat_* 给出拉伸前原始统计量的每通道摘要，便于回溯真实数值。
    """

    op: ReduceOp
    image: np.ndarray
    stat_min: list[float]
    stat_max: list[float]
    stat_mean: list[float]
    stretch_low_value: float
    stretch_high_value: float
    # 拉伸端点所在数值空间：raw=原始统计量；detrended_residual=去条纹
    # 零中心残差（0=符合行列趋势）；smoothed=邻域平滑后；可用 + 组合
    stretch_domain: str
    p_low: float
    p_high: float
    destripe: bool
    smooth: int
    rect: tuple[int, int, int, int] | None
    frame_range: FrameRange
    frames_analyzed: int
    width: int
    height: int


def _box_mean(stat: np.ndarray, k: int) -> np.ndarray:
    """k×k 邻域均值滤波（积分图实现，边缘复制填充），用于压制噪声粒度。"""
    pad_lo = k // 2
    pad_hi = k - 1 - pad_lo
    padded = np.pad(
        stat, ((pad_lo, pad_hi), (pad_lo, pad_hi), (0, 0)), mode="edge"
    )
    integral = np.pad(
        padded, ((1, 0), (1, 0), (0, 0))
    ).cumsum(axis=0).cumsum(axis=1)
    return (
        integral[k:, k:] - integral[:-k, k:]
        - integral[k:, :-k] + integral[:-k, :-k]
    ) / (k * k)


def _stretch(stat: np.ndarray, p_low: float, p_high: float
             ) -> tuple[np.ndarray, float, float]:
    """三通道联合百分位拉伸到 0-255，返回 (uint8 图, 低端点, 高端点)。"""
    lo = float(np.percentile(stat, p_low))
    hi = float(np.percentile(stat, p_high))
    if hi - lo < 1e-12:
        # 统计图全平（如纯色视频的 std）：输出中灰，端点保持真实值
        return np.full(stat.shape, 128, dtype=np.uint8), lo, hi
    scaled = np.clip((stat - lo) / (hi - lo), 0.0, 1.0)
    return (scaled * 255.0 + 0.5).astype(np.uint8), lo, hi


def temporal_reduce(
    path: Path,
    op: ReduceOp = "std",
    rect: tuple[int, int, int, int] | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
    p_low: float = 1.0,
    p_high: float = 99.0,
    destripe: bool = False,
    smooth: int = 0,
    max_median_bytes: int = DEFAULT_MAX_MEDIAN_BYTES,
    progress: ProgressCallback | None = None,
) -> TemporalReduceResult:
    """对帧序列做逐像素时间统计，返回统计图。视频只解码一遍。

    op：mean/median/min/max/std/diff（diff=相邻采样帧绝对差的时间均值，
    即运动能量）。rect 只统计子区域（也是 median 控内存的手段）。
    destripe=True 扣除统计图的逐列/逐行均值，抑制条纹伪影；
    smooth=N（>=2）对统计图做 N×N 邻域均值，压制噪声粒度、凸显区域结构。
    两者只影响可视化图像，统计摘要仍为处理前的真实数值。
    """
    if op not in REDUCE_OPS:
        raise InvalidRangeError(
            f"op {op!r} 无效，可选：{'/'.join(REDUCE_OPS)}"
        )
    if not (0.0 <= p_low < p_high <= 100.0):
        raise InvalidRangeError(
            f"百分位范围无效：p_low={p_low}, p_high={p_high}"
            "（要求 0 <= p_low < p_high <= 100）"
        )
    if not (0 <= smooth <= MAX_SMOOTH):
        raise InvalidRangeError(
            f"smooth {smooth} 无效，必须在 0～{MAX_SMOOTH} 内"
        )
    if smooth < 0:
        raise InvalidRangeError(f"smooth {smooth} 无效，必须 >= 0")

    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        width, height = info.width, info.height
        if rect is not None:
            validate_rect(*rect, width, height)

        frame_range = resolve_range(
            reader, start_frame, end_frame, start, end, sample_every
        )
        total = frame_range.count
        if op in ("std", "diff") and total < 2:
            raise InvalidRangeError(
                f"op={op} 至少需要两帧，请扩大帧范围或减小 sample_every"
            )

        rw = rect[2] if rect is not None else width
        rh = rect[3] if rect is not None else height
        if op == "median":
            estimated = total * rh * rw * 3 * 2  # uint8 帧堆栈 + 工作拷贝
            if estimated > max_median_bytes:
                raise InvalidRangeError(
                    f"median 需要一次持有全部采样帧，预计约 "
                    f"{estimated // (1024 * 1024)} MB，超过上限 "
                    f"{max_median_bytes // (1024 * 1024)} MB",
                    hint="请加大 sample_every、用 rect 缩小区域，或改用 mean/std",
                )

        acc_sum: np.ndarray | None = None
        acc_sq: np.ndarray | None = None
        acc_min: np.ndarray | None = None
        acc_max: np.ndarray | None = None
        acc_diff: np.ndarray | None = None
        prev: np.ndarray | None = None
        stack: list[np.ndarray] = []
        done = 0

        for _idx, _t, arr in reader.iter_frames(
            frame_range.start, frame_range.end, frame_range.sample_every
        ):
            if rect is not None:
                x, y, w, h = rect
                arr = np.ascontiguousarray(arr[y : y + h, x : x + w, :])

            if op == "mean":
                acc_sum = arr.astype(np.float64) if acc_sum is None \
                    else acc_sum + arr
            elif op == "std":
                f = arr.astype(np.float64)
                if acc_sum is None:
                    acc_sum, acc_sq = f, f * f
                else:
                    acc_sum += f
                    acc_sq += f * f
            elif op == "min":
                acc_min = arr.copy() if acc_min is None \
                    else np.minimum(acc_min, arr)
            elif op == "max":
                acc_max = arr.copy() if acc_max is None \
                    else np.maximum(acc_max, arr)
            elif op == "diff":
                if prev is not None:
                    # uint8 直接相减会下溢；max-min 与绝对差等价
                    d = np.maximum(arr, prev) - np.minimum(arr, prev)
                    acc_diff = d.astype(np.float64) if acc_diff is None \
                        else acc_diff + d
                prev = arr
            else:  # median
                stack.append(arr)

            done += 1
            if progress is not None:
                progress(done, total)

        if done == 0:
            raise DecodeError("指定范围内没有解码出任何帧")

        if op == "mean":
            assert acc_sum is not None
            stat = acc_sum / done
        elif op == "std":
            assert acc_sum is not None and acc_sq is not None
            mean = acc_sum / done
            var = np.maximum(acc_sq / done - mean * mean, 0.0)
            stat = np.sqrt(var)
        elif op == "min":
            assert acc_min is not None
            stat = acc_min.astype(np.float64)
        elif op == "max":
            assert acc_max is not None
            stat = acc_max.astype(np.float64)
        elif op == "diff":
            if acc_diff is None:
                raise InvalidRangeError(
                    "op=diff 至少需要两帧，请扩大帧范围或减小 sample_every"
                )
            stat = acc_diff / (done - 1)
        else:
            stat = np.median(np.stack(stack), axis=0).astype(np.float64)

        display_stat = stat
        if destripe:
            # 双向去趋势（零中心残差）：0=符合行列趋势，负=低于趋势（更静止），
            # 正=高于趋势。补回全局均值以消除"均值被减两次"的系统性偏移。
            display_stat = (
                display_stat
                - display_stat.mean(axis=0, keepdims=True)
                - display_stat.mean(axis=1, keepdims=True)
                + display_stat.mean(axis=(0, 1), keepdims=True)
            )
        if smooth >= 2:
            display_stat = _box_mean(display_stat, smooth)
        image, lo, hi = _stretch(display_stat, p_low, p_high)
        domain_parts = []
        if destripe:
            domain_parts.append("detrended_residual")
        if smooth >= 2:
            domain_parts.append("smoothed")
        return TemporalReduceResult(
            op=op,
            image=image,
            stat_min=[round(float(v), 4) for v in stat.min(axis=(0, 1))],
            stat_max=[round(float(v), 4) for v in stat.max(axis=(0, 1))],
            stat_mean=[round(float(v), 4) for v in stat.mean(axis=(0, 1))],
            stretch_low_value=round(lo, 4),
            stretch_high_value=round(hi, 4),
            stretch_domain="+".join(domain_parts) if domain_parts else "raw",
            p_low=p_low,
            p_high=p_high,
            destripe=destripe,
            smooth=smooth,
            rect=rect,
            frame_range=frame_range,
            frames_analyzed=done,
            width=width,
            height=height,
        )
