"""采样帧网格图（contact sheet）：一张图看完整段视频的代表帧。

plan_sheet_frames / compose_sheet 拆分为纯函数，供 media_scanner
在单遍解码循环中复用（避免重复解码）。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, sqrt
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import DecodeError, InvalidRangeError
from pixelprobe.output.image_writer import fit_within

ProgressCallback = Callable[[int, int], None]

# 标注条高度与网格间隙（像素）
_LABEL_BAR = 14
_GAP = 2
_BG = (24, 24, 28)

# 目标帧占范围比例低于该值时用 PTS seek 逐帧取，否则单遍顺序解码
_SEEK_DENSITY_LIMIT = 0.3


@dataclass
class ContactSheetResult:
    """采样帧网格结果。frames/times 与网格内从左到右、从上到下一一对应。"""

    image: np.ndarray
    frames: list[int]
    times: list[float]
    cols: int
    rows: int
    tile_width: int
    tile_height: int


def plan_sheet_frames(frame_range: FrameRange, count: int) -> list[int]:
    """在闭区间内等距选取 count 个帧号（去重、升序）。"""
    if count < 1:
        raise InvalidRangeError(f"count {count} 无效，必须 >= 1")
    positions = np.linspace(frame_range.start, frame_range.end, num=count)
    seen: list[int] = []
    for p in positions:
        f = int(round(p))
        if not seen or f != seen[-1]:
            seen.append(f)
    return seen


def compose_sheet(
    tiles: list[np.ndarray],
    frames: list[int],
    times: list[float],
    cols: int | None = None,
    tile_max_dim: int = 320,
    annotate: bool = True,
) -> ContactSheetResult:
    """把帧数组拼为网格图（可带帧号/秒标注条）。纯函数，不做解码。"""
    if not tiles:
        raise DecodeError("没有可拼接的帧")
    scaled = [fit_within(t, tile_max_dim, tile_max_dim) for t in tiles]
    tile_h, tile_w = scaled[0].shape[:2]
    n = len(scaled)
    cols = cols if cols is not None else ceil(sqrt(n))
    cols = max(1, min(cols, n))
    rows = ceil(n / cols)
    cell_h = tile_h + (_LABEL_BAR if annotate else 0)

    sheet_w = cols * tile_w + (cols - 1) * _GAP
    sheet_h = rows * cell_h + (rows - 1) * _GAP
    img = Image.new("RGB", (sheet_w, sheet_h), _BG)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    for i, tile in enumerate(scaled):
        r, c = divmod(i, cols)
        x = c * (tile_w + _GAP)
        y = r * (cell_h + _GAP)
        img.paste(Image.fromarray(np.ascontiguousarray(tile)), (x, y))
        if annotate:
            bar_top = y + tile_h
            draw.rectangle([x, bar_top, x + tile_w - 1, bar_top + _LABEL_BAR - 1],
                           fill=(0, 0, 0))
            # 标注一律 ASCII（PIL 默认字体不含中文）
            draw.text(
                (x + 2, bar_top + 1),
                f"f={frames[i]} t={times[i]:.2f}s",
                fill=(230, 230, 230), font=font,
            )
    return ContactSheetResult(
        image=np.asarray(img, dtype=np.uint8),
        frames=frames,
        times=times,
        cols=cols,
        rows=rows,
        tile_width=tile_w,
        tile_height=tile_h,
    )


def sample_frames(
    path: Path,
    count: int = 9,
    cols: int | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    tile_max_dim: int = 320,
    annotate: bool = True,
    progress: ProgressCallback | None = None,
) -> ContactSheetResult:
    """在指定范围内等距抽取 count 帧并拼成一张带标注的网格图。"""
    with VideoReader() as reader:
        reader.open(Path(path))
        frame_range = resolve_range(reader, start_frame, end_frame, start, end, 1)
        targets = plan_sheet_frames(frame_range, count)

        frames: list[int] = []
        times: list[float] = []
        tiles: list[np.ndarray] = []
        density = len(targets) / (frame_range.end - frame_range.start + 1)
        if density < _SEEK_DENSITY_LIMIT:
            for i, f in enumerate(targets):
                t, arr = reader.get_frame_by_index(f)
                frames.append(f)
                times.append(t)
                tiles.append(arr)
                if progress is not None:
                    progress(i + 1, len(targets))
        else:
            wanted = set(targets)
            done = 0
            for idx, t, arr in reader.iter_frames(
                frame_range.start, frame_range.end, 1
            ):
                if idx in wanted:
                    frames.append(idx)
                    times.append(t)
                    tiles.append(arr.copy())
                    done += 1
                    if progress is not None:
                        progress(done, len(targets))
                    if done == len(targets):
                        break

    if not tiles:
        raise DecodeError("指定范围内没有解码出任何帧")
    return compose_sheet(tiles, frames, times, cols, tile_max_dim, annotate)
