"""频域/周期分析：时间域主频检测 + 单帧空间频谱。

判断"噪声是否周期生成、画面是否周期闪烁、是否存在条纹/摩尔纹"。
时间域基于单遍解码的亮度或变化序列做 rfft；空间域对单帧灰度做 fft2。
频率相关性不等于语义结论，仍需结合原始帧视觉确认。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import numpy as np

from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.media_reader import load_frame
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import DecodeError, InvalidRangeError
from pixelprobe.compat.legacy_results import preview_image
from pixelprobe.domain.tensor import TensorField
from pixelprobe.operators.frequency import (
    make_spatial_fft_tensor,
    make_temporal_fft_tensor,
)
from pixelprobe.operators.preview import make_preview_tensor
from pixelprobe.output.plot import render_curve
from pixelprobe.utils.coordinates import validate_point, validate_rect

ProgressCallback = Callable[[int, int], None]

SpectrumSource = Literal["change", "luma"]

# 时间域 FFT 至少需要的样本数
_MIN_SAMPLES = 8
# 空间频谱屏蔽中心低频盲区的最小半径（像素）
_MIN_CENTER_BLOCK = 2
_TOP_PEAKS = 5


@dataclass
class TemporalSpectrumResult:
    """时间域频谱结果。频率单位 Hz（按实测平均帧间隔换算）。

    nyquist_hz 是可检测频率上限（有效采样率的一半）：sample_every > 1
    时高于该值的周期成分会混叠或完全漏采。
    """

    source: SpectrumSource
    dominant_freq_hz: float | None
    period_seconds: float | None
    period_frames: float | None
    peak_ratio: float
    top_peaks: list[dict]
    spectrum_image: np.ndarray
    vfr_warning: bool
    effective_fps: float
    nyquist_hz: float
    frame_range: FrameRange
    samples: int
    data_tensor: TensorField
    preview_tensor: TensorField


@dataclass
class SpatialSpectrumResult:
    """单帧空间频谱结果。angle_deg 为频率向量方向（条纹走向与其垂直）。"""

    spectrum_image: np.ndarray
    peaks: list[dict]
    frame: int | None
    time_seconds: float | None
    rect: tuple[int, int, int, int] | None
    width: int
    height: int
    data_tensor: TensorField
    preview_tensor: TensorField


def _luma(arr: np.ndarray) -> np.ndarray:
    return (
        0.299 * arr[..., 0].astype(np.float64)
        + 0.587 * arr[..., 1].astype(np.float64)
        + 0.114 * arr[..., 2].astype(np.float64)
    )


def temporal_spectrum(
    path: Path,
    source: SpectrumSource = "luma",
    rect: tuple[int, int, int, int] | None = None,
    point: tuple[int, int] | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
    progress: ProgressCallback | None = None,
) -> TemporalSpectrumResult:
    """检测亮度/变化序列的周期成分，返回主频与谱线图。视频只解码一遍。

    source=luma 分析区域（或整帧/单点）平均亮度随时间的波动；
    source=change 分析相邻采样帧变化量序列。rect 与 point 最多给一个。
    """
    if source not in ("change", "luma"):
        raise InvalidRangeError(f"source {source!r} 无效，可选 change/luma")
    if rect is not None and point is not None:
        raise InvalidRangeError("rect 与 point 最多只能指定一个")

    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        if rect is not None:
            validate_rect(*rect, info.width, info.height)
        if point is not None:
            validate_point(point[0], point[1], info.width, info.height)

        frame_range = resolve_range(
            reader, start_frame, end_frame, start, end, sample_every
        )
        total = frame_range.count
        values: list[float] = []
        times: list[float] = []
        prev: np.ndarray | None = None
        done = 0
        for _idx, t, arr in reader.iter_frames(
            frame_range.start, frame_range.end, frame_range.sample_every
        ):
            times.append(t)
            if rect is not None:
                x, y, w, h = rect
                arr = arr[y : y + h, x : x + w, :]
            elif point is not None:
                arr = arr[point[1] : point[1] + 1, point[0] : point[0] + 1, :]
            if source == "luma":
                values.append(float(_luma(arr).mean()))
            else:
                if prev is not None:
                    diff = np.maximum(arr, prev) - np.minimum(arr, prev)
                    values.append(float(diff.mean()))
                prev = arr
            done += 1
            if progress is not None:
                progress(done, total)

        if done == 0:
            raise DecodeError("指定范围内没有解码出任何帧")

    n = len(values)
    if n < _MIN_SAMPLES:
        raise InvalidRangeError(
            f"频谱分析至少需要 {_MIN_SAMPLES} 个采样值（当前 {n}），"
            "请扩大帧范围或减小 sample_every"
        )
    # 用解码得到的真实时间戳估计采样率并检测 VFR
    # （比容器元数据的粗略 is_vfr 可靠：直接量化帧间隔波动）
    intervals = np.diff(np.asarray(times, dtype=np.float64))
    mean_interval = float(intervals.mean()) if len(intervals) else 0.0
    if mean_interval <= 0:
        raise InvalidRangeError("无法从帧时间戳确定采样率，无法换算频率")
    effective_fps = 1.0 / mean_interval
    vfr = bool(
        len(intervals) >= 2
        and float(intervals.max() - intervals.min()) > 0.1 * mean_interval
    )
    fps = info.fps if info.fps and info.fps > 0 \
        else effective_fps * frame_range.sample_every

    series = np.asarray(values, dtype=np.float64)
    complex_spectrum = np.fft.rfft(series - series.mean())
    spectrum = np.abs(complex_spectrum)
    if n % 2 == 0:
        # 实信号单侧幅度谱中 Nyquist bin 不折半，天然双倍计权；
        # 减半后与其他 bin 可比，避免 dominant 判定偏向 Nyquist 频率
        spectrum[-1] *= 0.5
    freqs = np.fft.rfftfreq(n, d=mean_interval)
    body = spectrum[1:]  # 去掉直流分量
    body_freqs = freqs[1:]

    if body.sum() < 1e-9:
        dominant = None
        peak_ratio = 0.0
        top_peaks: list[dict] = []
    else:
        # 幅度降序；幅度并列时偏向低频（确定性）
        order = np.lexsort((np.arange(len(body)), -body))[:_TOP_PEAKS]
        dominant = int(order[0])
        peak_ratio = float(body[dominant] / body.sum())
        top_peaks = [
            {
                "freq_hz": round(float(body_freqs[i]), 4),
                "period_seconds": round(float(1.0 / body_freqs[i]), 4),
                "period_frames": round(float(fps / body_freqs[i]), 2),
                "magnitude": round(float(body[i]), 4),
            }
            for i in order
        ]

    image = render_curve(
        body.tolist(),
        markers=[dominant] if dominant is not None else None,
        y_min=0.0,
    )
    data_tensor = make_temporal_fft_tensor(
        complex_spectrum,
        freqs,
        source=source,
        vfr_compatibility_estimate=vfr,
    )
    preview_tensor = make_preview_tensor(
        image,
        tensor_id=f"preview_temporal_fft_{source}",
        source_tensor_id=data_tensor.tensor_id,
        source_width=max(len(body), 1),
        source_height=1,
        attributes={"visualization": "magnitude_curve", "dc_excluded": True},
    )
    return TemporalSpectrumResult(
        source=source,
        dominant_freq_hz=(
            round(float(body_freqs[dominant]), 4) if dominant is not None else None
        ),
        period_seconds=(
            round(float(1.0 / body_freqs[dominant]), 4)
            if dominant is not None else None
        ),
        period_frames=(
            round(float(fps / body_freqs[dominant]), 2)
            if dominant is not None else None
        ),
        peak_ratio=round(peak_ratio, 4),
        top_peaks=top_peaks,
        spectrum_image=preview_image(preview_tensor),
        vfr_warning=vfr,
        effective_fps=round(effective_fps, 4),
        nyquist_hz=round(effective_fps / 2.0, 4),
        frame_range=frame_range,
        samples=n,
        data_tensor=data_tensor,
        preview_tensor=preview_tensor,
    )


def spatial_spectrum(
    path: Path,
    frame: int | None = None,
    time: float | None = None,
    rect: tuple[int, int, int, int] | None = None,
) -> SpatialSpectrumResult:
    """对单帧（可裁剪）做二维频谱分析，检测条纹/周期纹理。

    返回中心化 log 幅度谱图与 top 峰列表；峰的 period_px 是条纹周期
    （像素），angle_deg 是频率向量方向（0=水平频率即垂直条纹）。
    """
    arr, idx, t, info = load_frame(Path(path), frame=frame, time=time)
    if rect is not None:
        validate_rect(*rect, info.width, info.height)
        x, y, w, h = rect
        arr = arr[y : y + h, x : x + w, :]
    gray = _luma(arr)
    h, w = gray.shape
    if h < 8 or w < 8:
        raise InvalidRangeError("空间频谱分析区域至少需要 8×8 像素")

    spectrum = np.fft.fftshift(np.fft.fft2(gray - gray.mean()))
    magnitude = np.abs(spectrum)
    log_mag = np.log1p(magnitude)
    peak = log_mag.max()
    display = (
        (log_mag / peak * 255.0).astype(np.uint8)
        if peak > 0 else np.zeros_like(log_mag, dtype=np.uint8)
    )
    image = np.repeat(display[:, :, None], 3, axis=2)

    cy, cx = h // 2, w // 2
    block = max(_MIN_CENTER_BLOCK, min(h, w) // 32)
    masked = magnitude.copy()
    masked[cy - block : cy + block + 1, cx - block : cx + block + 1] = 0.0

    # 偶数尺寸下 fftshift 后 Nyquist 行/列只有负半边且 bin 自共轭
    ny_u = -(w // 2) if w % 2 == 0 else None
    ny_v = -(h // 2) if h % 2 == 0 else None

    peaks: list[dict] = []
    top_value: float | None = None
    flat_order = np.argsort(masked.ravel())[::-1]
    for flat in flat_order:
        if len(peaks) >= _TOP_PEAKS:
            break
        value = float(masked.ravel()[flat])
        if value <= 0:
            break
        if top_value is not None and value < top_value * 1e-6:
            break  # FFT 数值残渣（~1e-12），不足以构成真实峰
        py, px = divmod(int(flat), w)
        u, v = px - cx, py - cy
        # 频谱共轭对称去重：保留 v<0 半平面代表；v=0 行与偶数尺寸的
        # Nyquist 行（v=-h/2）是自共轭行，只保留 u>=0 一侧——但 Nyquist
        # 列 bin（u=-w/2）自身即自共轭且正半边不存在，必须保留
        self_conjugate_row = v == 0 or (ny_v is not None and v == ny_v)
        if v > 0:
            continue
        if (self_conjugate_row and u < 0
                and (ny_u is None or u != ny_u)):
            continue
        fx, fy = u / w, v / h
        freq = float(np.hypot(fx, fy))
        if freq <= 0:
            continue
        peaks.append({
            "u": u,
            "v": v,
            "period_px": round(1.0 / freq, 2),
            "angle_deg": round(float(np.degrees(np.arctan2(v, u))), 2),
            "magnitude": round(float(value), 2),
        })
        if top_value is None:
            top_value = value

    data_tensor = make_spatial_fft_tensor(
        spectrum,
        source_width=info.width,
        source_height=info.height,
        rect=rect,
    )
    preview_tensor = make_preview_tensor(
        image,
        tensor_id="preview_spatial_fft_luma",
        source_tensor_id=data_tensor.tensor_id,
        source_width=w,
        source_height=h,
        attributes={
            "visualization": "log_magnitude",
            "fft_shifted": True,
        },
    )

    return SpatialSpectrumResult(
        spectrum_image=preview_image(preview_tensor),
        peaks=peaks,
        frame=idx,
        time_seconds=t,
        rect=rect,
        width=w,
        height=h,
        data_tensor=data_tensor,
        preview_tensor=preview_tensor,
    )
