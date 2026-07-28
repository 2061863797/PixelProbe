"""FFT 复数 Data Tensor 与频谱 Preview 的轴语义。"""

from __future__ import annotations

import numpy as np
from dataclasses import replace

from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.axes import AxisKind, AxisMapping, AxisSpec
from pixelprobe.domain.coordinates import CoordinateSpace, CoordinateSpaceKind
from pixelprobe.domain.references import ProvenanceRef
from pixelprobe.domain.tensor import MemoryArrayHandle, TensorField
from pixelprobe.operators.base import OperatorSpec
from pixelprobe.operators.common import memory_array_ref
from pixelprobe.output.plot import render_curve

_MIN_SAMPLES = 8
_MIN_CENTER_BLOCK = 2
_TOP_PEAKS = 5

FREQUENCY_OPERATOR_SPEC = OperatorSpec(
    name="frequency.fft",
    version="1.0.0",
    category="frequency",
    deterministic="tolerance",
    stateful=False,
    chunkable=False,
    cacheable=True,
    supported_dtypes=("float64", "complex128"),
    config_schema_id="pixelprobe.operator.frequency.fft.v1",
)


def make_temporal_fft_tensor(
    spectrum: np.ndarray,
    frequencies_hz: np.ndarray,
    *,
    source: str,
    vfr_compatibility_estimate: bool,
) -> TensorField:
    data = np.asarray(spectrum, dtype=np.complex128)
    frequencies = np.asarray(frequencies_hz, dtype=np.float64)
    if data.ndim != 1 or frequencies.shape != data.shape:
        raise ValueError("时间 FFT 与频率坐标必须是一维且等长")
    if len(frequencies) > 1:
        step = float(frequencies[1] - frequencies[0])
    else:
        step = 1.0
    accuracy = AccuracyInfo(
        level=(
            AccuracyLevel.ESTIMATED
            if vfr_compatibility_estimate
            else AccuracyLevel.DERIVED
        ),
        source=f"{FREQUENCY_OPERATOR_SPEC.name}:{FREQUENCY_OPERATOR_SPEC.version}",
        assumptions=(
            ("VFR compatibility mode uses mean frame interval",)
            if vfr_compatibility_estimate
            else ()
        ),
        unit="code_value",
    )
    return TensorField(
        tensor_id=f"tensor_temporal_fft_{source}",
        data=MemoryArrayHandle(data),
        axes=(AxisSpec(
            name="frequency",
            kind=AxisKind.FREQUENCY,
            length=len(data),
            unit="hertz",
            coordinate_mode="regular",
            start=float(frequencies[0]) if len(frequencies) else 0.0,
            step=step,
        ),),
        channels=(),
        coordinate_space=None,
        axis_mappings=(),
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id=f"prov_temporal_fft_{source}"),
        attributes={
            "artifact_role": "data",
            "source": source,
            "vfr_compatibility_estimate": vfr_compatibility_estimate,
            "requires_explicit_resampling": vfr_compatibility_estimate,
        },
    )


def make_spatial_fft_tensor(
    spectrum: np.ndarray,
    *,
    source_width: int,
    source_height: int,
    rect: tuple[int, int, int, int] | None,
) -> TensorField:
    data = np.asarray(spectrum, dtype=np.complex128)
    if data.ndim != 2:
        raise ValueError("空间 FFT 必须是二维复数数组")
    height, width = data.shape
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DERIVED,
        source=f"{FREQUENCY_OPERATOR_SPEC.name}:{FREQUENCY_OPERATOR_SPEC.version}",
        assumptions=("mean-centered luma",),
        unit="code_value",
    )
    return TensorField(
        tensor_id="tensor_spatial_fft_luma",
        data=MemoryArrayHandle(data),
        axes=(
            AxisSpec(
                name="frequency_y", kind=AxisKind.FREQUENCY,
                length=height, unit="cycle/pixel", coordinate_mode="regular",
                start=float(np.fft.fftshift(np.fft.fftfreq(height))[0]),
                step=1.0 / height,
            ),
            AxisSpec(
                name="frequency_x", kind=AxisKind.FREQUENCY,
                length=width, unit="cycle/pixel", coordinate_mode="regular",
                start=float(np.fft.fftshift(np.fft.fftfreq(width))[0]),
                step=1.0 / width,
            ),
        ),
        channels=(),
        coordinate_space=CoordinateSpace(
            coordinate_space_id="spatial_frequency",
            kind=CoordinateSpaceKind.NORMALIZED,
            axes=("frequency_x", "frequency_y"),
            width=width, height=height, unit="cycle/pixel",
            parent_space_id="storage_pixels",
        ),
        axis_mappings=(), validity=None, accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id="prov_spatial_fft_luma"),
        attributes={
            "artifact_role": "data", "source_width": source_width,
            "source_height": source_height, "rect": rect,
        },
    )


def stft_from_series(
    values: list[float],
    times: tuple[float, ...],
    *,
    source: str,
    window: str,
    length: int,
    hop: int,
    padding: str,
    normalization: str,
) -> TensorField:
    """对规则时间序列生成完整复数 STFT Data Tensor。"""
    if len(values) != len(times):
        raise ValueError("STFT 数值与时间坐标长度不一致")
    if length < 2 or hop < 1:
        raise ValueError("STFT length 必须至少为 2，hop 必须至少为 1")
    if window not in {"hann", "hamming", "rect"}:
        raise ValueError("STFT window 可选 hann/hamming/rect")
    if padding not in {"none", "end", "center"}:
        raise ValueError("STFT padding 可选 none/end/center")
    if normalization not in {"none", "window_sum", "window_energy"}:
        raise ValueError(
            "STFT normalization 可选 none/window_sum/window_energy"
        )
    if len(values) < 2:
        raise ValueError("STFT 至少需要两个时间采样")
    intervals = np.diff(np.asarray(times, dtype=np.float64))
    mean_interval = float(intervals.mean())
    if mean_interval <= 0 or not np.allclose(
        # 容许容器 time base 量化造成的 1 tick 抖动；真正 VFR 仍会失败。
        intervals, mean_interval, rtol=0.05, atol=1e-9,
    ):
        raise ValueError("STFT 要求规则且严格递增的时间间隔")
    series = np.asarray(values, dtype=np.float64)
    start_time = float(times[0])
    if padding == "center":
        left = length // 2
        right = length - 1 - left
        series = np.pad(series, (left, right))
        start_time -= left * mean_interval
    elif padding == "end" and len(series) < length:
        series = np.pad(series, (0, length - len(series)))
    if padding == "none" and len(series) < length:
        raise ValueError("STFT 输入长度小于窗口；请选择 end/center padding")
    starts = list(range(0, max(len(series) - length + 1, 1), hop))
    if padding == "end":
        last_start = starts[-1] if starts else 0
        if last_start + length < len(series):
            starts.append(last_start + hop)
    if not starts:
        starts = [0]
    required = starts[-1] + length
    if required > len(series):
        series = np.pad(series, (0, required - len(series)))
    window_values = {
        "hann": np.hanning,
        "hamming": np.hamming,
        "rect": lambda size: np.ones(size, dtype=np.float64),
    }[window](length).astype(np.float64)
    divisor = 1.0
    if normalization == "window_sum":
        divisor = float(window_values.sum())
    elif normalization == "window_energy":
        divisor = float(np.sqrt(np.square(window_values).sum()))
    if divisor <= 0:
        raise ValueError("STFT 窗函数归一化因子无效")
    spectra = np.stack([
        np.fft.rfft(series[start:start + length] * window_values) / divisor
        for start in starts
    ]).astype(np.complex128, copy=False)
    frequencies = np.fft.rfftfreq(length, d=mean_interval)
    center_times = np.asarray([
        start_time + (start + (length - 1) / 2.0) * mean_interval
        for start in starts
    ], dtype=np.float64)
    time_ref = memory_array_ref(center_times, f"index_tensor_stft_{source}_time")
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DERIVED,
        source=f"{FREQUENCY_OPERATOR_SPEC.name}:{FREQUENCY_OPERATOR_SPEC.version}",
        assumptions=(f"window={window}", f"padding={padding}"),
        unit="code_value",
    )
    return TensorField(
        tensor_id=f"tensor_stft_{source}",
        data=MemoryArrayHandle(spectra),
        axes=(
            AxisSpec(
                name="window_time", kind=AxisKind.TIME,
                length=len(starts), unit="second",
                coordinate_mode="irregular", coordinates_ref=time_ref,
                mapping_id=f"map_tensor_stft_{source}_time",
            ),
            AxisSpec(
                name="frequency", kind=AxisKind.FREQUENCY,
                length=len(frequencies), unit="hertz",
                coordinate_mode="regular", start=float(frequencies[0]),
                step=(
                    float(frequencies[1] - frequencies[0])
                    if len(frequencies) > 1 else 1.0
                ),
            ),
        ),
        channels=(), coordinate_space=None,
        axis_mappings=(AxisMapping(
            mapping_id=f"map_tensor_stft_{source}_time", kind="lookup",
            input_artifact_id="source_media", input_axes=("time",),
            output_artifact_id=f"tensor_stft_{source}",
            output_axes=("window_time",),
            parameters={
                "coordinates_ref": time_ref.artifact_id,
                "window_length": length, "hop": hop,
            },
            accuracy=accuracy,
        ),),
        validity=None, accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id=f"prov_stft_{source}"),
        attributes={
            "artifact_role": "data", "source": source,
            "window": window, "length": length, "hop": hop,
            "padding": padding, "normalization": normalization,
            "_index_values": {time_ref.artifact_id: center_times.tolist()},
        },
    )
def temporal_fft_from_series(
    values: list[float],
    times: tuple[float, ...],
    *,
    source: str,
    nominal_fps: float | None,
    sample_every: int,
    allow_vfr_estimate: bool,
) -> TensorField:
    count = len(values)
    if count < _MIN_SAMPLES:
        raise ValueError(f"频谱分析至少需要 {_MIN_SAMPLES} 个采样值（当前 {count}）")
    intervals = np.diff(np.asarray(times, dtype=np.float64))
    mean_interval = float(intervals.mean()) if len(intervals) else 0.0
    if mean_interval <= 0:
        raise ValueError("无法从帧时间戳确定采样率")
    effective_fps = 1.0 / mean_interval
    vfr = bool(
        len(intervals) >= 2
        and float(intervals.max() - intervals.min()) > 0.1 * mean_interval
    )
    if vfr and not allow_vfr_estimate:
        raise ValueError("时间 FFT 要求规则时间轴；VFR 必须先显式重采样")
    fps = nominal_fps if nominal_fps and nominal_fps > 0 else effective_fps * sample_every
    series = np.asarray(values, dtype=np.float64)
    complex_spectrum = np.fft.rfft(series - series.mean())
    magnitude = np.abs(complex_spectrum)
    if count % 2 == 0:
        magnitude[-1] *= 0.5
    frequencies = np.fft.rfftfreq(count, d=mean_interval)
    body = magnitude[1:]
    body_frequencies = frequencies[1:]
    if body.sum() < 1e-9:
        dominant = None
        peak_ratio = 0.0
        top_peaks: list[dict[str, float]] = []
    else:
        order = np.lexsort((np.arange(len(body)), -body))[:_TOP_PEAKS]
        dominant = int(order[0])
        peak_ratio = float(body[dominant] / body.sum())
        top_peaks = [
            {
                "freq_hz": round(float(body_frequencies[index]), 4),
                "period_seconds": round(float(1.0 / body_frequencies[index]), 4),
                "period_frames": round(float(fps / body_frequencies[index]), 2),
                "magnitude": round(float(body[index]), 4),
            }
            for index in order
        ]
    tensor = make_temporal_fft_tensor(
        complex_spectrum, frequencies, source=source,
        vfr_compatibility_estimate=vfr,
    )
    attributes = {
        **tensor.attributes,
        "samples": count,
        "effective_fps": round(effective_fps, 4),
        "nyquist_hz": round(effective_fps / 2.0, 4),
        "vfr_warning": vfr,
        "dominant_index": dominant,
        "dominant_freq_hz": (
            round(float(body_frequencies[dominant]), 4) if dominant is not None else None
        ),
        "period_seconds": (
            round(float(1.0 / body_frequencies[dominant]), 4) if dominant is not None else None
        ),
        "period_frames": (
            round(float(fps / body_frequencies[dominant]), 2) if dominant is not None else None
        ),
        "peak_ratio": round(peak_ratio, 4),
        "top_peaks": top_peaks,
    }
    return replace(tensor, attributes=attributes)


def temporal_fft_preview(tensor: TensorField) -> np.ndarray:
    spectrum = np.abs(tensor.data.materialize())
    if tensor.attributes["samples"] % 2 == 0:
        spectrum[-1] *= 0.5
    body = spectrum[1:]
    dominant = tensor.attributes.get("dominant_index")
    return render_curve(
        body.tolist(),
        markers=[int(dominant)] if dominant is not None else None,
        y_min=0.0,
    )


def spatial_fft_from_frame(
    frame: np.ndarray,
    *,
    source_width: int,
    source_height: int,
    rect: tuple[int, int, int, int] | None,
    frame_index: int,
    time_seconds: float,
) -> TensorField:
    values = frame
    if rect is not None:
        x, y, width, height = rect
        values = values[y:y + height, x:x + width, :]
    gray = (
        0.299 * values[..., 0].astype(np.float64)
        + 0.587 * values[..., 1].astype(np.float64)
        + 0.114 * values[..., 2].astype(np.float64)
    )
    height, width = gray.shape
    if height < 8 or width < 8:
        raise ValueError("空间频谱分析区域至少需要 8×8 像素")
    spectrum = np.fft.fftshift(np.fft.fft2(gray - gray.mean()))
    magnitude = np.abs(spectrum)
    center_y, center_x = height // 2, width // 2
    block = max(_MIN_CENTER_BLOCK, min(height, width) // 32)
    masked = magnitude.copy()
    masked[
        center_y - block:center_y + block + 1,
        center_x - block:center_x + block + 1,
    ] = 0.0
    nyquist_x = -(width // 2) if width % 2 == 0 else None
    nyquist_y = -(height // 2) if height % 2 == 0 else None
    peaks: list[dict[str, float | int]] = []
    top_value = None
    for flat in np.argsort(masked.ravel())[::-1]:
        if len(peaks) >= _TOP_PEAKS:
            break
        value = float(masked.ravel()[flat])
        if value <= 0 or (top_value is not None and value < top_value * 1e-6):
            break
        peak_y, peak_x = divmod(int(flat), width)
        u, v = peak_x - center_x, peak_y - center_y
        self_conjugate_row = v == 0 or (nyquist_y is not None and v == nyquist_y)
        if v > 0 or (
            self_conjugate_row and u < 0 and (nyquist_x is None or u != nyquist_x)
        ):
            continue
        frequency = float(np.hypot(u / width, v / height))
        if frequency <= 0:
            continue
        peaks.append({
            "u": u, "v": v,
            "period_px": round(1.0 / frequency, 2),
            "angle_deg": round(float(np.degrees(np.arctan2(v, u))), 2),
            "magnitude": round(value, 2),
        })
        if top_value is None:
            top_value = value
    tensor = make_spatial_fft_tensor(
        spectrum, source_width=source_width, source_height=source_height, rect=rect,
    )
    return replace(tensor, attributes={
        **tensor.attributes,
        "frame": frame_index,
        "time_seconds": time_seconds,
        "width": width,
        "height": height,
        "peaks": peaks,
    })


def spatial_fft_preview(tensor: TensorField) -> np.ndarray:
    log_magnitude = np.log1p(np.abs(tensor.data.materialize()))
    maximum = float(log_magnitude.max())
    display = (
        (log_magnitude / maximum * 255.0).astype(np.uint8)
        if maximum > 0 else np.zeros_like(log_magnitude, dtype=np.uint8)
    )
    return np.repeat(display[..., None], 3, axis=2)
