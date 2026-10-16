"""带刻度图片：缩放比例、max_side 上限与网格。"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

from pixelprobe.output.annotate import Axis, annotate, frame_axis


def _shown_size(result) -> tuple[int, int]:
    meta = result.meta
    return round(meta["source_width"] * meta["scale_x"]), round(meta["source_height"] * meta["scale_y"])


def test_small_image_is_upscaled_by_integer_factor() -> None:
    result = annotate(np.zeros((16, 32, 3), dtype=np.uint8), x_axis=Axis("x", 32), y_axis=Axis("y", 16))
    assert result.meta["scale_x"] == result.meta["scale_y"] == 8.0
    assert result.meta["resampling"] == "nearest"
    assert _shown_size(result) == (256, 128)


def test_upscale_never_exceeds_max_side() -> None:
    image = np.zeros((32, 32, 3), dtype=np.uint8)
    for max_side, expected in ((64, 2.0), (100, 3.0), (32, 1.0), (1024, 8.0)):
        result = annotate(image, max_side=max_side)
        assert result.meta["scale_x"] == expected, max_side
        assert max(_shown_size(result)) <= max(max_side, 32)


def test_large_image_is_downscaled_to_max_side_and_grid_is_labelled() -> None:
    result = annotate(np.zeros((600, 1200, 3), dtype=np.uint8), grid=True, max_side=600)
    assert _shown_size(result) == (600, 300)
    assert result.meta["resampling"] == "box_average"
    assert result.meta["cell_grid"]["columns"] == 8
    with Image.open(io.BytesIO(result.png)) as png:
        assert png.size == (result.width, result.height)


def test_frame_axis_labels_rows_with_frame_numbers() -> None:
    axis = frame_axis([5, 9, 30])
    assert [axis.text(i) for i in range(3)] == ["f5", "f9", "f30"]
    assert Axis("dx", 64, offset=-32).text(0) == "-32"
