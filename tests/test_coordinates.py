"""坐标工具单元测试：坐标与越界。"""

from __future__ import annotations

import pytest

from pixelprobe.models.errors import (
    CoordinateOutOfRangeError,
    InvalidRangeError,
)
from pixelprobe.utils.coordinates import (
    grid_points,
    parse_point,
    parse_rect,
    pixel_id_from_xy,
    validate_point,
    validate_rect,
    xy_from_pixel_id,
)


def test_pixel_id_roundtrip() -> None:
    width, height = 1920, 1080
    for x, y in [(0, 0), (1919, 0), (0, 1079), (1919, 1079), (520, 340)]:
        pid = pixel_id_from_xy(x, y, width)
        assert pid == y * width + x
        assert xy_from_pixel_id(pid, width, height) == (x, y)


def test_pixel_id_out_of_range() -> None:
    with pytest.raises(CoordinateOutOfRangeError):
        xy_from_pixel_id(16 * 16, 16, 16)
    with pytest.raises(CoordinateOutOfRangeError):
        xy_from_pixel_id(-1, 16, 16)


def test_parse_point() -> None:
    assert parse_point("520,340") == (520, 340)
    assert parse_point(" 3 , 5 ") == (3, 5)
    with pytest.raises(InvalidRangeError):
        parse_point("520")
    with pytest.raises(InvalidRangeError):
        parse_point("a,b")


def test_parse_rect() -> None:
    assert parse_rect("400,200,300,300") == (400, 200, 300, 300)
    with pytest.raises(InvalidRangeError):
        parse_rect("400,200,300")
    with pytest.raises(InvalidRangeError):
        parse_rect("0,0,0,10")  # width < 1


def test_validate_point_corners() -> None:
    # 四角必须合法
    for x, y in [(0, 0), (15, 0), (0, 15), (15, 15)]:
        validate_point(x, y, 16, 16)
    # 越界必须报错
    for x, y in [(-1, 0), (16, 0), (0, 16)]:
        with pytest.raises(CoordinateOutOfRangeError):
            validate_point(x, y, 16, 16)


def test_validate_rect() -> None:
    validate_rect(0, 0, 16, 16, 16, 16)
    with pytest.raises(CoordinateOutOfRangeError):
        validate_rect(1, 0, 16, 16, 16, 16)  # 超出右边界
    with pytest.raises(CoordinateOutOfRangeError):
        validate_rect(-1, 0, 4, 4, 16, 16)


def test_grid_points_row_major() -> None:
    pts = grid_points((10, 20, 5, 5), 2)
    # 行优先：先 x 后 y
    assert pts == [
        (10, 20), (12, 20), (14, 20),
        (10, 22), (12, 22), (14, 22),
        (10, 24), (12, 24), (14, 24),
    ]
    with pytest.raises(InvalidRangeError):
        grid_points((0, 0, 4, 4), 0)
