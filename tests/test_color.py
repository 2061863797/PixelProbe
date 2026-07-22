"""颜色换算单元测试。"""

from __future__ import annotations

import numpy as np

from pixelprobe.core.color import (
    luminance_array,
    luminance_linear_array,
    rgb_to_hex,
    rgb_to_hsv_array,
    rgb_to_lab_array,
)


def test_rgb_to_hex() -> None:
    assert rgb_to_hex(255, 0, 0) == "#FF0000"
    assert rgb_to_hex(0, 0, 0) == "#000000"
    assert rgb_to_hex(16, 32, 48) == "#102030"


def test_hsv_known_colors() -> None:
    colors = np.array(
        [
            [255, 0, 0],    # 红 → H=0, S=100, V=100
            [0, 255, 0],    # 绿 → H=120
            [0, 0, 255],    # 蓝 → H=240
            [255, 255, 0],  # 黄 → H=60
            [255, 255, 255],  # 白 → S=0, V=100
            [0, 0, 0],      # 黑 → V=0
            [128, 128, 128],  # 灰 → S=0
        ],
        dtype=np.uint8,
    )
    hsv = rgb_to_hsv_array(colors)
    np.testing.assert_allclose(hsv[0], [0.0, 100.0, 100.0], atol=0.01)
    np.testing.assert_allclose(hsv[1], [120.0, 100.0, 100.0], atol=0.01)
    np.testing.assert_allclose(hsv[2], [240.0, 100.0, 100.0], atol=0.01)
    np.testing.assert_allclose(hsv[3], [60.0, 100.0, 100.0], atol=0.01)
    assert hsv[4][1] == 0.0 and hsv[4][2] == 100.0
    assert hsv[5][2] == 0.0
    assert hsv[6][1] == 0.0


def test_hsv_range() -> None:
    rng = np.random.default_rng(42)
    arr = rng.integers(0, 256, size=(100, 3), dtype=np.uint8)
    hsv = rgb_to_hsv_array(arr)
    assert (hsv[:, 0] >= 0).all() and (hsv[:, 0] < 360.0 + 1e-9).all()
    assert (hsv[:, 1] >= 0).all() and (hsv[:, 1] <= 100.0).all()
    assert (hsv[:, 2] >= 0).all() and (hsv[:, 2] <= 100.0).all()


def test_luminance() -> None:
    arr = np.array(
        [[255, 255, 255], [0, 0, 0], [255, 0, 0]], dtype=np.uint8
    )
    lum = luminance_array(arr)
    np.testing.assert_allclose(lum[0], 255.0, atol=1e-6)
    assert lum[1] == 0.0
    np.testing.assert_allclose(lum[2], 0.2126 * 255, atol=1e-6)


def test_luminance_linear() -> None:
    arr = np.array(
        [[255, 255, 255], [0, 0, 0], [255, 0, 0], [128, 128, 128]],
        dtype=np.uint8,
    )
    lum = luminance_linear_array(arr)
    np.testing.assert_allclose(lum[0], 255.0, atol=0.01)
    assert lum[1] == 0.0
    # 纯红线性亮度 = 0.2126729 × 255
    np.testing.assert_allclose(lum[2], 0.2126729 * 255, atol=0.01)
    # 中灰 128：sRGB 0.502 → 线性 ≈ 0.2159
    np.testing.assert_allclose(lum[3], 0.21586 * 255, atol=0.1)


def test_lab_known_colors() -> None:
    arr = np.array(
        [[255, 255, 255], [0, 0, 0], [255, 0, 0], [0, 255, 0]],
        dtype=np.uint8,
    )
    lab = rgb_to_lab_array(arr)
    # 白：L=100，a≈0，b≈0
    np.testing.assert_allclose(lab[0], [100.0, 0.0, 0.0], atol=0.01)
    # 黑：全 0
    np.testing.assert_allclose(lab[1], [0.0, 0.0, 0.0], atol=0.01)
    # sRGB 纯红 / 纯绿的标准 CIELAB 值（D65）
    np.testing.assert_allclose(lab[2], [53.24, 80.09, 67.20], atol=0.05)
    np.testing.assert_allclose(lab[3], [87.74, -86.18, 83.18], atol=0.05)
