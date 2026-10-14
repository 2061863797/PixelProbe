"""统一地址：命令行 `--at` 与 MCP 共用的时间、位置写法。详见 grammar 模块说明。"""

from pixelprobe.address.grammar import GRAMMAR_HINT, parse_address
from pixelprobe.address.model import (
    Address,
    Cell,
    Col,
    Grid,
    Path,
    PixelId,
    Point,
    Rect,
    Row,
    TimeSpec,
)
from pixelprobe.address.resolve import (
    cell_rect,
    check,
    col_of,
    grid_of,
    needs_media_size,
    pair_options,
    points_of,
    range_options,
    rect_of,
    row_of,
    single_frame_options,
)

__all__ = [
    "Address", "Cell", "Col", "GRAMMAR_HINT", "Grid", "Path", "PixelId", "Point",
    "Rect", "Row", "TimeSpec", "cell_rect", "check", "col_of", "grid_of",
    "needs_media_size", "pair_options", "parse_address", "points_of",
    "range_options", "rect_of", "row_of", "single_frame_options",
]
