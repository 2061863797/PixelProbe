"""把 Address 换算为各命令已有的时间、坐标参数。

每个命令先用 `check()` 声明自己接受的时间写法和形状；不符合时给出
"这个命令可以怎么写"的明确错误。换算本身不读取媒体，只有 cell(...) 和 pid(...)
需要画面尺寸。
"""

from __future__ import annotations

from collections.abc import Iterable

from pixelprobe.address.model import (
    Address,
    Cell,
    Col,
    Grid,
    PixelId,
    Point,
    Rect,
    Row,
    shape_name,
)
from pixelprobe.models.errors import AddressError
from pixelprobe.utils.coordinates import xy_from_pixel_id

_TIME_MODE_TEXT = {
    "single": "单帧（f=7 或 t=0.5s）",
    "range": "连续范围（f=3..25 或 t=1s..3s）",
    "list": "两帧（f=0,5 或 t=0s,0.5s）",
    "all": "全部（f=all）",
}
_SHAPE_TEXT = {
    "point": "point(x,y)", "pid": "pid(n)", "rect": "rect(x,y,w,h)",
    "grid": "grid(x,y,w,h step=n)", "row": "row(y)", "col": "col(x)",
    "cell": "cell(C4)", "line": "line(x1,y1 -> x2,y2)", "path": "path(...)",
}


def check(
    address: Address,
    *,
    command: str,
    time_modes: Iterable[str] = (),
    shapes: Iterable[str] = (),
    step: bool = False,
    max_regions: int = 1,
) -> None:
    """校验地址是否适用于该命令。

    max_regions 限制 rect/cell/grid/row/col/line/path 这类"区域"形状的个数；
    point/pid 可以有多个，由命令自行决定是否接受。
    """
    allowed_modes = set(time_modes)
    allowed_shapes = set(shapes)
    if address.time is not None:
        mode = address.time.mode
        if mode not in allowed_modes:
            if not allowed_modes:
                raise AddressError(f"{command} 的地址不接受时间（收到 {address.time}）")
            options = "、".join(_TIME_MODE_TEXT[item] for item in sorted(allowed_modes))
            raise AddressError(
                f"{command} 的地址时间应为：{options}；收到 {address.time}",
            )
        if mode == "list" and len(address.time.values) != 2:
            raise AddressError(f"{command} 需要恰好两帧，收到 {address.time}")
    if address.step is not None and not step:
        raise AddressError(f"{command} 的地址不接受 step=")
    regions = 0
    for shape in address.shapes:
        name = shape_name(shape)
        if name not in allowed_shapes:
            if not allowed_shapes:
                raise AddressError(f"{command} 的地址不接受空间位置（收到 {shape}）")
            options = "、".join(_SHAPE_TEXT[item] for item in sorted(allowed_shapes))
            raise AddressError(
                f"{command} 的地址不支持 {shape}；可用：{options}",
            )
        if name not in {"point", "pid"}:
            regions += 1
    if regions > max_regions:
        raise AddressError(f"{command} 的地址最多只能有 {max_regions} 个区域")
    point_like = any(shape_name(shape) in {"point", "pid"} for shape in address.shapes)
    if point_like and regions:
        raise AddressError(f"{command} 的地址不能同时指定点和区域")


def range_options(address: Address) -> dict[str, object]:
    """换算为闭区间帧范围参数：start_frame/end_frame 或 start/end，外加 sample_every。"""
    options: dict[str, object] = {
        "start_frame": None, "end_frame": None, "start": None, "end": None,
        "sample_every": address.step or 1,
    }
    spec = address.time
    if spec is None or spec.mode == "all":
        return options
    if spec.mode == "list":
        raise AddressError(f"这里需要连续范围，收到帧列表 {spec}")
    first, last = (spec.values[0], spec.values[0]) if spec.mode == "single" else spec.values
    if spec.kind == "frame":
        options["start_frame"], options["end_frame"] = first, last
    else:
        options["start"], options["end"] = first, last
    return options


def single_frame_options(address: Address) -> dict[str, object]:
    """换算为单帧参数 frame 或 time；未指定时间时两者都为 None（命令自行决定默认帧）。"""
    spec = address.time
    if spec is None:
        return {"frame": None, "time": None}
    if spec.mode != "single":
        raise AddressError(f"这里只接受单帧（f=7 或 t=0.5s），收到 {spec}")
    value = spec.values[0]
    if spec.kind == "frame":
        return {"frame": value, "time": None}
    return {"frame": None, "time": value}


def pair_options(address: Address) -> dict[str, object]:
    """换算为两帧参数 frame_a/time_a 与 frame_b/time_b。"""
    spec = address.time
    if spec is None or spec.mode != "list" or len(spec.values) != 2:
        raise AddressError("这里需要恰好两帧，写成 f=0,5 或 t=0s,0.5s")
    first, second = spec.values
    if spec.kind == "frame":
        return {"frame_a": first, "time_a": None, "frame_b": second, "time_b": None}
    return {"frame_a": None, "time_a": first, "frame_b": None, "time_b": second}


def cell_rect(cell: Cell, width: int, height: int) -> tuple[int, int, int, int]:
    """把格子换算为像素矩形。格子边界按 floor(i * 尺寸 / 格数) 划分，各格无缝拼满整幅画面。"""
    if cell.columns > width or cell.rows > height:
        raise AddressError(
            f"{cell.columns}x{cell.rows} 网格大于画面 {width}x{height}，请换更粗的网格",
        )
    x0 = cell.column_start * width // cell.columns
    x1 = (cell.column_end + 1) * width // cell.columns
    y0 = cell.row_start * height // cell.rows
    y1 = (cell.row_end + 1) * height // cell.rows
    return x0, y0, x1 - x0, y1 - y0


def rect_of(address: Address, width: int, height: int) -> tuple[int, int, int, int] | None:
    """取唯一的 rect(...) 或 cell(...)，换算为 (x, y, w, h)；没有时返回 None。"""
    for shape in address.shapes:
        if isinstance(shape, Rect):
            return shape.x, shape.y, shape.width, shape.height
        if isinstance(shape, Cell):
            return cell_rect(shape, width, height)
    return None


def points_of(address: Address, width: int, height: int) -> list[tuple[int, int]]:
    """按书写顺序取所有 point(...) 与 pid(...)，pid 换算为坐标。"""
    points: list[tuple[int, int]] = []
    for shape in address.shapes:
        if isinstance(shape, Point):
            points.append((shape.x, shape.y))
        elif isinstance(shape, PixelId):
            points.append(xy_from_pixel_id(shape.pixel_id, width, height))
    return points


def grid_of(address: Address) -> Grid | None:
    for shape in address.shapes:
        if isinstance(shape, Grid):
            return shape
    return None


def row_of(address: Address) -> int | None:
    for shape in address.shapes:
        if isinstance(shape, Row):
            return shape.y
    return None


def col_of(address: Address) -> int | None:
    for shape in address.shapes:
        if isinstance(shape, Col):
            return shape.x
    return None


def needs_media_size(address: Address) -> bool:
    return any(isinstance(shape, (Cell, PixelId)) for shape in address.shapes)
