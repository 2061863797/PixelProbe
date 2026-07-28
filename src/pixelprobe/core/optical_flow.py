"""光流分析：稠密运动场、全局运动估计与消除（需可选依赖 OpenCV）。

模块顶层不 import cv2：无 [flow] extra 的环境中 CLI 命令注册不受影响，
调用时通过 require_cv2() 得到带安装提示的 DEPENDENCY_MISSING 错误。
cv2 只做数值计算，不做任何文件 IO（帧数据全部来自 PyAV）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import DecodeError, InvalidRangeError
from pixelprobe.compat.legacy_results import preview_image
from pixelprobe.domain.tensor import TensorField
from pixelprobe.operators.optical_flow import (
    make_flow_tensor,
    make_magnitude_tensor,
)
from pixelprobe.operators.preview import make_preview_tensor
from pixelprobe.output.plot import apply_colormap
from pixelprobe.utils.optional_deps import require_cv2

ProgressCallback = Callable[[int, int], None]

# 估计全局仿射时的网格采样步长（像素）
_AFFINE_GRID_STEP = 8


@dataclass
class FlowResult:
    """光流结果。位移单位为像素；角度 0°=向右，y 向下为正方向。

    dominant_angle_deg 是运动区域（幅度 > mag_threshold）内的幅度加权
    主方向；无运动区域或区域内向量相互抵消（如两块反向运动）时为 None。
    """

    flow_image: np.ndarray
    magnitude_image: np.ndarray
    mean_magnitude: float
    max_magnitude: float
    p95_magnitude: float
    dominant_angle_deg: float | None
    global_motion: dict | None
    compensated: bool
    motion_bbox: tuple[int, int, int, int] | None
    mag_threshold: float
    frame_a: int
    frame_b: int
    accumulated: bool
    frames_analyzed: int
    frame_range: FrameRange | None
    width: int
    height: int
    raw_flow_tensor: TensorField
    flow_tensor: TensorField
    magnitude_tensor: TensorField
    flow_preview_tensor: TensorField
    magnitude_preview_tensor: TensorField


def _gray(arr: np.ndarray) -> np.ndarray:
    return (
        0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
    ).astype(np.float32)


def _farneback(cv2, prev: np.ndarray, cur: np.ndarray) -> np.ndarray:
    return cv2.calcOpticalFlowFarneback(
        prev, cur, None,
        pyr_scale=0.5, levels=3, winsize=15,
        iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
    )


def _estimate_global(cv2, flow: np.ndarray) -> dict | None:
    """从稠密流网格采样估计全局仿射（平移/旋转/缩放）。"""
    h, w = flow.shape[:2]
    ys, xs = np.mgrid[0:h:_AFFINE_GRID_STEP, 0:w:_AFFINE_GRID_STEP]
    src = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(np.float32)
    dst = src + flow[ys.ravel(), xs.ravel()].astype(np.float32)
    if len(src) < 3:
        return None
    matrix, _inliers = cv2.estimateAffinePartial2D(src, dst)
    if matrix is None:
        return None
    a, b = float(matrix[0, 0]), float(matrix[1, 0])
    return {
        "dx": round(float(matrix[0, 2]), 4),
        "dy": round(float(matrix[1, 2]), 4),
        "rotation_deg": round(float(np.degrees(np.arctan2(b, a))), 4),
        "scale": round(float(np.hypot(a, b)), 6),
        "matrix": [[round(float(v), 6) for v in row] for row in matrix],
    }


def _subtract_global(flow: np.ndarray, matrix: list[list[float]]) -> np.ndarray:
    """从流场中扣除全局仿射预测的逐像素位移。"""
    h, w = flow.shape[:2]
    m = np.asarray(matrix, dtype=np.float64)
    ys, xs = np.mgrid[0:h, 0:w]
    pred_x = m[0, 0] * xs + m[0, 1] * ys + m[0, 2] - xs
    pred_y = m[1, 0] * xs + m[1, 1] * ys + m[1, 2] - ys
    result = flow.astype(np.float64).copy()
    result[..., 0] -= pred_x
    result[..., 1] -= pred_y
    return result


def compute_flow(
    path: Path,
    frame_a: int | None = None,
    time_a: float | None = None,
    frame_b: int | None = None,
    time_b: float | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
    accumulate: bool = False,
    compensate_global: bool = False,
    mag_threshold: float = 1.0,
    progress: ProgressCallback | None = None,
) -> FlowResult:
    """计算稠密光流（Farneback）。

    两种模式：
    - 两帧模式（缺省）：frame_a/time_a 与 frame_b/time_b 各自二选一；
    - 累积模式（accumulate=True）：对帧范围逐对光流相加，视频只解码一遍。
    compensate_global=True 时估计全局仿射运动（镜头平移/旋转/缩放）并从
    流场中扣除，用于区分"镜头运动"和"物体运动"。
    """
    cv2 = require_cv2()
    if mag_threshold < 0:
        raise InvalidRangeError(f"mag_threshold {mag_threshold} 无效，必须 >= 0")

    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        width, height = info.width, info.height

        if accumulate:
            if any(v is not None for v in (frame_a, time_a, frame_b, time_b)):
                raise InvalidRangeError(
                    "累积模式使用帧范围参数，不能同时指定 frame_a/b 或 time_a/b"
                )
            frame_range = resolve_range(
                reader, start_frame, end_frame, start, end, sample_every
            )
            if frame_range.count < 2:
                raise InvalidRangeError(
                    "累积光流至少需要两帧，请扩大帧范围或减小 sample_every"
                )
            total = frame_range.count
            flow_sum: np.ndarray | None = None
            prev_gray: np.ndarray | None = None
            first_idx = last_idx = frame_range.start
            done = 0
            for idx, _t, arr in reader.iter_frames(
                frame_range.start, frame_range.end, frame_range.sample_every
            ):
                gray = _gray(arr)
                if prev_gray is None:
                    first_idx = idx
                else:
                    pair = _farneback(cv2, prev_gray, gray)
                    flow_sum = pair if flow_sum is None else flow_sum + pair
                prev_gray = gray
                last_idx = idx
                done += 1
                if progress is not None:
                    progress(done, total)
            if flow_sum is None:
                # 范围解析为 >=2 帧但实际解码不足（文件截断等）
                raise DecodeError("累积光流实际解码不足两帧，无法计算")
            flow = flow_sum
            idx_a, idx_b = first_idx, last_idx
            frames_analyzed = done
        else:
            if any(v is not None for v in (start_frame, end_frame, start, end)):
                raise InvalidRangeError(
                    "两帧模式使用 frame_a/b 或 time_a/b，不能同时指定帧范围参数"
                )
            frame_range = None
            idx_a, arr_a = _resolve(reader, "a", frame_a, time_a)
            idx_b, arr_b = _resolve(reader, "b", frame_b, time_b)
            flow = _farneback(cv2, _gray(arr_a), _gray(arr_b))
            frames_analyzed = 2

    raw_flow = flow.astype(np.float32, copy=True)
    global_motion = _estimate_global(cv2, flow)
    if compensate_global and global_motion is not None:
        flow = _subtract_global(flow, global_motion["matrix"])

    fx = flow[..., 0].astype(np.float64)
    fy = flow[..., 1].astype(np.float64)
    magnitude = np.hypot(fx, fy)
    mean_mag = float(magnitude.mean())
    max_mag = float(magnitude.max())

    mask = magnitude > mag_threshold
    bbox: tuple[int, int, int, int] | None = None
    dominant: float | None = None
    if mask.any():
        ys, xs = np.nonzero(mask)
        bbox = (
            int(xs.min()), int(ys.min()),
            int(xs.max() - xs.min()) + 1, int(ys.max() - ys.min()) + 1,
        )
        # 主方向 = 运动区域内的向量和方向（即幅度加权平均方向）。
        # 平均向量模过小（反向运动对消、纯噪声）时不给出方向，避免误导
        sum_fx = float(fx[mask].sum())
        sum_fy = float(fy[mask].sum())
        if np.hypot(sum_fx, sum_fy) / int(mask.sum()) > 0.05:
            dominant = float(np.degrees(np.arctan2(sum_fy, sum_fx)))

    # HSV 方向着色：hue=方向，value=幅度
    hsv = np.zeros((*magnitude.shape, 3), dtype=np.uint8)
    angle = np.degrees(np.arctan2(fy, fx)) % 360.0
    hsv[..., 0] = (angle / 2.0).astype(np.uint8)  # cv2 HSV hue 0-179
    hsv[..., 1] = 255
    scale = 255.0 / max_mag if max_mag > 1e-9 else 0.0
    hsv[..., 2] = np.clip(magnitude * scale, 0, 255).astype(np.uint8)
    flow_image = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    magnitude_image = apply_colormap(
        np.clip(magnitude * scale, 0, 255).astype(np.uint8), "fire"
    )

    compensated_applied = bool(compensate_global and global_motion is not None)
    raw_flow_tensor = make_flow_tensor(
        raw_flow,
        tensor_id="tensor_flow_raw",
        frame_a=idx_a,
        frame_b=idx_b,
        compensated=False,
    )
    flow_tensor = (
        make_flow_tensor(
            flow,
            tensor_id="tensor_flow_compensated",
            frame_a=idx_a,
            frame_b=idx_b,
            compensated=True,
        )
        if compensated_applied
        else raw_flow_tensor
    )
    magnitude_tensor = make_magnitude_tensor(
        magnitude,
        source_flow_tensor_id=flow_tensor.tensor_id,
    )
    flow_preview_tensor = make_preview_tensor(
        flow_image,
        tensor_id="preview_flow_direction",
        source_tensor_id=flow_tensor.tensor_id,
        source_width=width,
        source_height=height,
        attributes={"visualization": "hsv_direction"},
    )
    magnitude_preview_tensor = make_preview_tensor(
        magnitude_image,
        tensor_id="preview_flow_magnitude",
        source_tensor_id=magnitude_tensor.tensor_id,
        source_width=width,
        source_height=height,
        attributes={"visualization": "fire_colormap"},
    )

    return FlowResult(
        flow_image=preview_image(flow_preview_tensor),
        magnitude_image=preview_image(magnitude_preview_tensor),
        mean_magnitude=round(mean_mag, 4),
        max_magnitude=round(max_mag, 4),
        p95_magnitude=round(float(np.percentile(magnitude, 95)), 4),
        dominant_angle_deg=(
            round(dominant, 2) if dominant is not None else None
        ),
        global_motion=global_motion,
        compensated=compensated_applied,
        motion_bbox=bbox,
        mag_threshold=mag_threshold,
        frame_a=idx_a,
        frame_b=idx_b,
        accumulated=accumulate,
        frames_analyzed=frames_analyzed,
        frame_range=frame_range,
        width=width,
        height=height,
        raw_flow_tensor=raw_flow_tensor,
        flow_tensor=flow_tensor,
        magnitude_tensor=magnitude_tensor,
        flow_preview_tensor=flow_preview_tensor,
        magnitude_preview_tensor=magnitude_preview_tensor,
    )


def _resolve(
    reader: VideoReader, label: str,
    frame: int | None, time: float | None,
) -> tuple[int, np.ndarray]:
    if (frame is None) == (time is None):
        raise InvalidRangeError(
            f"帧 {label} 必须且只能指定 frame_{label} 或 time_{label} 之一"
        )
    if frame is not None:
        _t, arr = reader.get_frame_by_index(frame)
        return frame, arr
    assert time is not None
    idx, _t, arr = reader.get_frame_by_time(time)
    return idx, arr
