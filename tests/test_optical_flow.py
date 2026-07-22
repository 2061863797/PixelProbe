"""光流分析测试：运动方向、bbox、累积模式、缺依赖错误。"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    MOTION_BLOCK,
    MOTION_FRAME_COUNT,
    MOTION_STEP,
    MOTION_X0,
    MOTION_Y,
)
from pixelprobe.models.errors import DependencyMissingError, InvalidRangeError

cv2 = pytest.importorskip("cv2")

from pixelprobe.core import compute_flow  # noqa: E402


def test_rightward_motion_direction(motion_video: Path) -> None:
    result = compute_flow(motion_video, frame_a=0, frame_b=4)
    # 白块每帧右移 2px：主方向约 0°（向右）
    assert result.dominant_angle_deg == pytest.approx(0.0, abs=15.0)
    assert result.max_magnitude > 2.0
    assert result.motion_bbox is not None
    x, y, w, h = result.motion_bbox
    # 运动区域应覆盖白块轨迹（起点 x=4 → 帧 4 时 x=12..20）
    assert y <= MOTION_Y + MOTION_BLOCK and y + h >= MOTION_Y
    assert x <= MOTION_X0 + 4 * MOTION_STEP + MOTION_BLOCK


def test_accumulate_mode(motion_video: Path) -> None:
    result = compute_flow(
        motion_video, accumulate=True,
        start_frame=0, end_frame=MOTION_FRAME_COUNT - 1,
    )
    assert result.accumulated is True
    assert result.frames_analyzed == MOTION_FRAME_COUNT
    assert result.frame_a == 0
    assert result.frame_b == MOTION_FRAME_COUNT - 1
    assert result.dominant_angle_deg == pytest.approx(0.0, abs=20.0)


def test_global_motion_estimated(motion_video: Path) -> None:
    result = compute_flow(motion_video, frame_a=0, frame_b=1)
    assert result.global_motion is not None
    assert "dx" in result.global_motion
    # 局部小块运动：全局平移应接近 0（背景静止占主导）
    assert abs(result.global_motion["dx"]) < 1.0


def test_mode_conflicts_rejected(motion_video: Path) -> None:
    with pytest.raises(InvalidRangeError):
        compute_flow(motion_video, frame_a=0, frame_b=1, start_frame=0)
    with pytest.raises(InvalidRangeError):
        compute_flow(motion_video, accumulate=True, frame_a=0, frame_b=1)
    with pytest.raises(InvalidRangeError):
        compute_flow(motion_video, frame_a=0)  # 帧 B 未指定


def test_static_pair_reports_no_direction(motion_video: Path) -> None:
    """同一帧与自身比较：无运动区域时主方向必须为 None 而非噪声角度。"""
    result = compute_flow(motion_video, frame_a=0, frame_b=0)
    assert result.dominant_angle_deg is None
    assert result.motion_bbox is None


def test_flow_images_shapes(motion_video: Path) -> None:
    result = compute_flow(motion_video, frame_a=0, frame_b=1)
    assert result.flow_image.shape == (64, 64, 3)
    assert result.magnitude_image.shape == (64, 64, 3)


def test_dependency_missing_error(motion_video: Path, monkeypatch) -> None:
    """无 cv2 环境的行为：工具应返回 DEPENDENCY_MISSING 且提示安装 extra。"""
    import pixelprobe.core.optical_flow as of

    def missing():
        raise DependencyMissingError(
            "光流分析需要 OpenCV，但当前环境未安装",
            hint='pip install "pixelprobe[flow]"',
        )

    monkeypatch.setattr(of, "require_cv2", missing)
    with pytest.raises(DependencyMissingError) as excinfo:
        of.compute_flow(motion_video, frame_a=0, frame_b=1)
    assert excinfo.value.code == "DEPENDENCY_MISSING"
    assert "pixelprobe[flow]" in (excinfo.value.hint or "")
