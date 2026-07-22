"""频域分析测试：闪烁主频、VFR 警告、空间条纹峰。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from conftest import BLINK_RECT, run_json
from pixelprobe.core import spatial_spectrum, temporal_spectrum
from pixelprobe.models.errors import InvalidRangeError


def test_blink_dominant_frequency(blink_video: Path) -> None:
    """30fps、每 6 帧亮一次 → 主频 5Hz。"""
    x, y, w, h = BLINK_RECT
    result = temporal_spectrum(blink_video, source="luma", rect=(x, y, w, h))
    assert result.dominant_freq_hz == pytest.approx(5.0, abs=0.3)
    assert result.period_frames == pytest.approx(6.0, abs=0.4)
    assert result.peak_ratio > 0.1
    assert result.spectrum_image.shape == (256, 768, 3)


def test_change_source_also_periodic(blink_video: Path) -> None:
    result = temporal_spectrum(blink_video, source="change")
    # 变化序列亮/灭各产生一个尖峰，周期成分仍在 5Hz 或其谐波
    assert result.dominant_freq_hz is not None
    assert result.dominant_freq_hz >= 4.5


def test_flat_series_has_no_dominant(compat_video: Path) -> None:
    # 兼容视频每帧一个灰阶，亮度单调递增：无明显周期主峰要求不报错即可
    result = temporal_spectrum(compat_video, source="luma")
    assert result.samples == 10


def test_vfr_warning(vfr_video: Path) -> None:
    result = temporal_spectrum(vfr_video, source="luma")
    assert result.vfr_warning is True


def test_minimum_samples_enforced(test_video: Path) -> None:
    with pytest.raises(InvalidRangeError):
        temporal_spectrum(test_video, start_frame=0, end_frame=3)


def test_rect_point_exclusive(test_video: Path) -> None:
    with pytest.raises(InvalidRangeError):
        temporal_spectrum(test_video, rect=(0, 0, 4, 4), point=(1, 1))


def test_spatial_spectrum_detects_stripes(tmp_path: Path) -> None:
    """竖条纹（周期 8px 水平频率）应出现在峰列表首位。"""
    width = height = 64
    xs = np.arange(width)
    stripe = (127 + 120 * np.sin(2 * np.pi * xs / 8)).astype(np.uint8)
    arr = np.repeat(stripe[None, :], height, axis=0)
    image_path = tmp_path / "条纹.png"
    Image.fromarray(np.repeat(arr[:, :, None], 3, axis=2)).save(image_path)

    result = spatial_spectrum(image_path)
    assert result.peaks, "未检出任何频谱峰"
    top = result.peaks[0]
    assert top["period_px"] == pytest.approx(8.0, abs=0.5)
    assert abs(top["angle_deg"]) < 1e-6  # 水平频率向量 → 垂直条纹


def test_cli_spectrum_and_2d(blink_video: Path, tmp_path: Path) -> None:
    data = run_json(
        "spectrum", blink_video, "--rect", ",".join(map(str, BLINK_RECT)),
        "--json",
    )["data"]
    assert abs(data["dominant_freq_hz"] - 5.0) < 0.3

    out = tmp_path / "空间谱.png"
    data2 = run_json(
        "spectrum2d", blink_video, "--frame", 0, "--output", out, "--json"
    )["data"]
    assert data2["width"] == 32
    assert out.exists()
