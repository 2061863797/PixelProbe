"""output.plot 渲染件测试：曲线图与伪彩 LUT 的确定性行为。"""

from __future__ import annotations

import numpy as np
import pytest

from pixelprobe.output.plot import apply_colormap, render_curve


def test_render_curve_shape_and_line() -> None:
    image = render_curve([0.0, 1.0, 0.5], width=100, height=50)
    assert image.shape == (50, 100, 3)
    assert image.dtype == np.uint8
    # 曲线颜色（绿）出现在图中
    assert (image == np.array([80, 220, 120])).all(axis=2).any()


def test_render_curve_spans_and_markers() -> None:
    image = render_curve(
        [0.0] * 10, width=100, height=40, markers=[5], spans=[(1, 3)]
    )
    assert (image == np.array([240, 120, 80])).all(axis=2).any()  # marker
    assert (image == np.array([90, 40, 40])).all(axis=2).any()  # span
    with pytest.raises(ValueError):
        render_curve([])


def test_apply_colormap_endpoints() -> None:
    gray = np.array([[0, 128, 255]], dtype=np.uint8)
    fire = apply_colormap(gray, "fire")
    assert fire.shape == (1, 3, 3)
    assert fire[0, 0].tolist() == [0, 0, 0]        # 黑
    assert fire[0, 2].tolist() == [255, 255, 255]  # 白
    assert fire[0, 1][0] == 255                    # 中段偏红黄
    plain = apply_colormap(gray, "gray")
    assert np.array_equal(plain[..., 0], gray)
    with pytest.raises(ValueError):
        apply_colormap(gray, "jet")
    with pytest.raises(ValueError):
        apply_colormap(np.zeros((2, 2, 3), dtype=np.uint8))
