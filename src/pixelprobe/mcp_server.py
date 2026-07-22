"""PixelProbe MCP Server（stdio）。

把 pixelprobe.core 的确定性媒体分析能力包装为 MCP 工具，供 Claude 等
AI Agent 调用：先用少量帧、像素时间线、区域变化和 X–T/Y–T 切片定位重点，
再只查看必要的片段，从而不必完整反复观看视频。

分析工具只读、无副作用，帧图与切片图直接以图像内容返回给模型；
磁盘写入由独立的 save 工具完成。坐标/帧号/时间规范与 CLI 完全一致：
原点左上角、帧号从 0 起、范围为闭区间、时间单位秒。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image as PILImage
from pydantic import BaseModel, ConfigDict, Field

from mcp.server.fastmcp import FastMCP, Image

from pixelprobe import core
from pixelprobe.models.errors import PixelProbeError
from pixelprobe.output.image_writer import fit_within, save_png, scale_nearest
from pixelprobe.utils.coordinates import parse_point, parse_rect
from pixelprobe.utils.timecode import seconds_to_ms

mcp = FastMCP("pixelprobe_mcp")

# 返回给模型的图像最大边长（像素）：过大浪费上下文，过小看不清
DEFAULT_MAX_DIM = 768
HARD_MAX_DIM = 1568
# include_values 允许返回的最大数值格数（K×T）
MAX_INLINE_VALUES = 2000
# 切片/时间线自动放大目标：最短边不小于该值时不再放大
AUTO_SCALE_TARGET = 256


# ---------- 公共工具函数 ----------


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def _error_text(exc: PixelProbeError) -> str:
    msg = f"Error[{exc.code}]: {exc.message}"
    if exc.hint:
        msg += f"（提示：{exc.hint}）"
    return msg


def _png_image(arr: np.ndarray) -> Image:
    """把 [H,W,3] uint8 RGB 数组编码为 MCP 图像内容。"""
    buf = io.BytesIO()
    PILImage.fromarray(np.ascontiguousarray(arr)).save(buf, format="PNG")
    return Image(data=buf.getvalue(), format="png")


def _auto_scale(arr: np.ndarray) -> tuple[np.ndarray, int]:
    """对过小的切片/时间线图做最近邻整数放大，便于模型观察。

    返回 (放大后数组, 放大倍数)。
    """
    h, w = arr.shape[:2]
    longest = max(h, w)
    if longest >= AUTO_SCALE_TARGET:
        return arr, 1
    scale = min(16, max(1, AUTO_SCALE_TARGET // longest))
    return scale_nearest(arr, scale, scale), scale


def _save_png(arr: np.ndarray, output_path: str) -> str:
    """保存 PNG；目标存在时由 atomic_output 原子覆盖。"""
    save_png(arr, Path(output_path))
    return str(Path(output_path))


def _parse_points(points: Optional[list[str]]) -> Optional[list[tuple[int, int]]]:
    if not points:
        return None
    return [parse_point(p) for p in points]


def _parse_rect_opt(rect: Optional[str]) -> Optional[tuple[int, int, int, int]]:
    return parse_rect(rect) if rect else None


# ---------- 输入模型 ----------


class MediaPathInput(BaseModel):
    """仅需媒体路径的输入。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(
        ..., description="图片或视频文件路径（支持中文路径），例如 C:/videos/input.mp4",
        min_length=1,
    )


class _FrameBase(BaseModel):
    """取帧与保存帧共用参数。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., description="视频（或图片）路径", min_length=1)
    frame: Optional[int] = Field(
        default=None, description="帧号（从 0 开始），与 time 二选一", ge=0,
    )
    time: Optional[float] = Field(
        default=None, description="时间（秒，允许小数），与 frame 二选一", ge=0,
    )
    crop: Optional[str] = Field(
        default=None,
        description="裁剪区域 'x,y,width,height'（原始像素坐标），例如 '400,200,300,300'",
    )


class FrameInput(_FrameBase):
    """只读取帧输入。"""

    max_dim: int = Field(
        default=DEFAULT_MAX_DIM,
        description=f"返回图像的最大边长（像素），默认 {DEFAULT_MAX_DIM}；分析像素请用 inspect_pixels，不要靠放大图目测",
        ge=16, le=HARD_MAX_DIM,
    )


class SaveFrameInput(_FrameBase):
    """把指定帧保存到磁盘。"""

    output_path: str = Field(
        ..., description="PNG 输出路径；目标存在时覆盖", min_length=1,
    )


class InspectPixelsInput(BaseModel):
    """像素查询输入。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., description="图片或视频路径", min_length=1)
    points: list[str] = Field(
        ..., description="像素坐标列表，每项 'x,y'，例如 ['520,340','600,400']",
        min_length=1, max_length=200,
    )
    frame: Optional[int] = Field(
        default=None, description="视频帧号（从 0 开始），与 time 二选一", ge=0,
    )
    time: Optional[float] = Field(
        default=None, description="视频时间（秒），与 frame 二选一", ge=0,
    )


class AnalyzeRegionInput(BaseModel):
    """区域统计输入。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., description="图片或视频路径", min_length=1)
    rect: str = Field(
        ..., description="矩形区域 'x,y,width,height'，例如 '400,200,200,150'",
    )
    frame: Optional[int] = Field(
        default=None, description="视频帧号（从 0 开始），与 time 二选一", ge=0,
    )
    time: Optional[float] = Field(
        default=None, description="视频时间（秒），与 frame 二选一", ge=0,
    )


class _RangeMixin(BaseModel):
    """帧范围/时间范围公共字段（闭区间；帧与秒不能混用）。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    start_frame: Optional[int] = Field(
        default=None, description="起始帧（含），与 start/end 秒范围不能混用", ge=0,
    )
    end_frame: Optional[int] = Field(
        default=None, description="结束帧（含）", ge=0,
    )
    start: Optional[float] = Field(
        default=None, description="起始时间（秒），与帧范围不能混用", ge=0,
    )
    end: Optional[float] = Field(default=None, description="结束时间（秒）", ge=0)
    sample_every: int = Field(
        default=1, description="每隔 N 帧采样一次（长视频建议加大以省时间）", ge=1,
    )


class _TimelineBase(_RangeMixin):
    """时间线分析与保存共用参数。"""

    path: str = Field(..., description="视频路径", min_length=1)
    points: Optional[list[str]] = Field(
        default=None,
        description="像素坐标列表，每项 'x,y'；与 grid 二选一", max_length=500,
    )
    grid: Optional[str] = Field(
        default=None,
        description="网格采样区域 'x,y,width,height'；与 points 二选一",
    )
    step: Optional[int] = Field(
        default=None, description="网格采样步长（与 grid 搭配）", ge=1,
    )
    block_size: Optional[int] = Field(
        default=None,
        description="像素块边长 N：每个采样位置取 N×N 块平均 RGB（与 grid 搭配）", ge=1,
    )


class TimelineInput(_TimelineBase):
    """只读时间线输入：points 与 grid 二选一。"""

    include_values: bool = Field(
        default=False,
        description=f"是否在 JSON 中内联返回矩阵数值（仅当 K×T <= {MAX_INLINE_VALUES} 时允许）",
    )


class SaveTimelineInput(_TimelineBase):
    """把时间线图保存到磁盘。"""

    output_path: str = Field(
        ..., description="PNG 输出路径；目标存在时覆盖", min_length=1,
    )


class SliceInput(_RangeMixin):
    """X–T / Y–T 切片输入。"""

    path: str = Field(..., description="视频路径", min_length=1)
    coordinate: int = Field(
        ...,
        description="扫描线坐标：xt 工具填 y（水平线），yt 工具填 x（垂直线）", ge=0,
    )


class SaveSliceInput(SliceInput):
    """把 X–T / Y–T 切片保存到磁盘。"""

    output_path: str = Field(
        ..., description="PNG 输出路径；目标存在时覆盖", min_length=1,
    )


class DetectChangesInput(_RangeMixin):
    """变化检测输入：point / rect / grid 三选一。"""

    path: str = Field(..., description="视频路径", min_length=1)
    point: Optional[str] = Field(
        default=None, description="单像素坐标 'x,y'",
    )
    rect: Optional[str] = Field(
        default=None, description="矩形区域 'x,y,width,height'",
    )
    grid: Optional[str] = Field(
        default=None, description="网格采样区域 'x,y,width,height'（配合 step）",
    )
    step: Optional[int] = Field(
        default=None, description="网格采样步长（与 grid 搭配）", ge=1,
    )
    top: int = Field(default=10, description="返回变化最大的前 N 帧", ge=1, le=100)


def _load_frame(params: _FrameBase):
    crop = _parse_rect_opt(params.crop)
    return core.get_frame(
        Path(params.path), frame=params.frame, time=params.time, crop=crop
    )


def _extract_timeline(params: _TimelineBase):
    return core.extract_timelines(
        Path(params.path),
        points=_parse_points(params.points),
        grid=_parse_rect_opt(params.grid),
        step=params.step,
        block_size=params.block_size,
        start_frame=params.start_frame,
        end_frame=params.end_frame,
        start=params.start,
        end=params.end,
        sample_every=params.sample_every,
    )


def _create_slice(params: SliceInput, slice_type: str):
    fn = core.create_xt_slice if slice_type == "xt" else core.create_yt_slice
    return fn(
        Path(params.path), params.coordinate,
        params.start_frame, params.end_frame,
        params.start, params.end, params.sample_every,
    )


# ---------- 工具 ----------


@mcp.tool(
    name="pixelprobe_get_media_info",
    annotations={
        "title": "读取媒体信息",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_get_media_info(params: MediaPathInput) -> str:
    """读取图片或视频的基本信息，是分析任何媒体的第一步。

    返回 JSON：media_type、width、height、fps、frame_count（可能为估算值，
    见 frame_count_estimated）、duration_seconds、codec、pixel_format、
    is_vfr（可变帧率检测）等。后续所有坐标/帧号参数都应基于这里返回的
    尺寸和帧数范围。

    Returns:
        str: MediaInfo 的 JSON 字符串；失败时返回 "Error[CODE]: ..." 文本。
    """
    try:
        return _json(core.get_media_info(Path(params.path)).model_dump())
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_extract_frame",
    annotations={
        "title": "提取并查看指定帧",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_extract_frame(params: FrameInput):
    """提取视频指定帧（或图片本身）并直接以图像返回，供模型查看。

    用于"只看必要的帧"：先用 pixelprobe_detect_changes / timeline / 切片
    定位重点帧号，再用本工具查看该帧。支持 crop 裁剪聚焦局部。
    返回内容为 [JSON 元数据, PNG 图像]；元数据含 frame、time_seconds、
    原始尺寸与返回图像尺寸。注意：返回图像可能按 max_dim 缩小过，
    精确像素值请用 pixelprobe_inspect_pixels（始终基于原始分辨率）。
    """
    try:
        arr, idx, t, info = _load_frame(params)
        preview = fit_within(arr, params.max_dim, params.max_dim)
        # 小画面/小裁剪自动最近邻放大，便于模型观察。
        preview, display_scale = _auto_scale(preview)
        preview = fit_within(preview, params.max_dim, params.max_dim)
        meta = {
            "frame": idx,
            "time_seconds": t,
            "time_ms": seconds_to_ms(t) if t is not None else None,
            "source_width": info.width,
            "source_height": info.height,
            "crop": params.crop or None,
            "returned_width": int(preview.shape[1]),
            "returned_height": int(preview.shape[0]),
            "display_scale": display_scale,
        }
        return [_json(meta), _png_image(preview)]
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_inspect_pixels",
    annotations={
        "title": "查询像素精确颜色",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_inspect_pixels(params: InspectPixelsInput) -> str:
    """读取一个或多个像素的精确 RGB/HEX/HSV/亮度（始终基于原始分辨率）。

    视频用 frame 或 time 指定帧（都不给默认第 0 帧）。坐标原点在左上角。
    坐标越界会返回有效范围提示。

    Returns:
        str: JSON，含 frame、time_seconds 及 pixels 列表
        （每项：x,y,pixel_id,rgb{r,g,b},hex,hsv{h:0-360,s:0-100,v:0-100},luminance)。
    """
    try:
        arr, idx, t, _info = core.load_frame(
            Path(params.path), frame=params.frame, time=params.time
        )
        samples = core.inspect_pixels(
            arr, _parse_points(params.points) or [], frame=idx, time_seconds=t
        )
        return _json(
            {
                "frame": idx,
                "time_seconds": t,
                "pixels": [s.model_dump() for s in samples],
            }
        )
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_analyze_region",
    annotations={
        "title": "矩形区域统计",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_analyze_region(params: AnalyzeRegionInput) -> str:
    """计算某帧矩形区域的颜色统计（均值/中位数/最值/标准差/HSV/亮度）。

    用于快速了解一块区域"整体是什么颜色、是否均匀"，比逐像素查询省得多。

    Returns:
        str: JSON，含 frame、time_seconds 与 statistics
        （rect、pixel_count、mean_rgb、median_rgb、min_rgb、max_rgb、
        std_rgb、mean_hsv、mean_luminance、std_luminance）。
    """
    try:
        rect = parse_rect(params.rect)
        arr, idx, t, _info = core.load_frame(
            Path(params.path), frame=params.frame, time=params.time
        )
        stats = core.analyze_region(arr, rect)
        return _json(
            {
                "frame": idx,
                "time_seconds": t,
                "statistics": stats.model_dump(),
            }
        )
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_extract_timeline",
    annotations={
        "title": "像素颜色时间线",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_extract_timeline(params: TimelineInput):
    """提取固定像素随时间的颜色矩阵 [K,T,3]，并以图像返回（横轴=时间，纵轴=像素点）。

    这是"不看视频找变化"的核心工具：图中颜色突变的竖直位置即事件发生
    时刻。points（明确坐标）与 grid（区域网格采样，配 step 或 block_size）
    二选一。视频只解码一次，长视频请用 sample_every 降采样。
    返回 [JSON 元数据, PNG 时间线图]；元数据的 frames/times 给出每一列
    对应的帧号与秒数（图像可能被整数倍放大，倍数见 display_scale）。
    include_values=true 时内联返回矩阵数值（小规模时用于精确比对）。
    """
    try:
        result = _extract_timeline(params)
        raw = result.matrix  # [K,T,3]：本身即"行=像素点，列=时间"的图像
        display, scale = _auto_scale(raw)
        display = fit_within(display, HARD_MAX_DIM, HARD_MAX_DIM)
        meta: dict[str, Any] = {
            "k_points": len(result.points),
            "t_frames": len(result.frames),
            "points": [p.model_dump() for p in result.points],
            "frames": result.frames,
            "times": result.times,
            "sample_type": result.sample_type,
            "block_size": result.block_size,
            "raw_width": int(raw.shape[1]),
            "raw_height": int(raw.shape[0]),
            "display_scale": scale,
            "axis": "横轴=时间（左早右晚），纵轴=像素点（自上而下按 points 顺序）",
        }
        if params.include_values:
            if raw.shape[0] * raw.shape[1] > MAX_INLINE_VALUES:
                meta["values_omitted"] = (
                    f"K×T={raw.shape[0] * raw.shape[1]} 超过 {MAX_INLINE_VALUES}，"
                    "请缩小范围、加大 sample_every 或减少采样点后重试"
                )
            else:
                meta["values"] = raw.tolist()
        return [_json(meta), _png_image(display)]
    except PixelProbeError as exc:
        return _error_text(exc)


def _slice_impl(params: SliceInput, slice_type: str):
    try:
        result = _create_slice(params, slice_type)
        raw = result.array  # [T, 空间, 3]
        display, scale = _auto_scale(raw)
        display = fit_within(display, HARD_MAX_DIM, HARD_MAX_DIM)
        meta = {
            "slice_type": slice_type,
            "fixed_coordinate": result.fixed_coordinate,
            "start_frame": result.frame_range.start,
            "end_frame": result.frame_range.end,
            "sample_every": result.frame_range.sample_every,
            "t_frames": len(result.frames),
            "frames_first": result.frames[0],
            "frames_last": result.frames[-1],
            "space_axis": "original_x" if slice_type == "xt" else "original_y",
            "axis": (
                "纵轴=时间（上早下晚，每行一帧），横轴="
                + ("原视频 x" if slice_type == "xt" else "原视频 y")
            ),
            "raw_width": int(raw.shape[1]),
            "raw_height": int(raw.shape[0]),
            "display_scale": scale,
        }
        return [_json(meta), _png_image(display)]
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_xt_slice",
    annotations={
        "title": "X–T 时空切片",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_xt_slice(params: SliceInput):
    """生成固定水平扫描线（coordinate=y）的 X–T 切片图并返回。

    每帧取第 y 行，按时间自上而下堆叠：水平运动的物体呈斜线，
    静止物体呈竖直条带，全画面事件（闪光/切镜头）呈水平横条。
    斜线起点所在行即运动开始的帧（行号 = frames 列表下标）。
    返回 [JSON 元数据, PNG 图像]。
    """
    return _slice_impl(params, "xt")


@mcp.tool(
    name="pixelprobe_yt_slice",
    annotations={
        "title": "Y–T 时空切片",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_yt_slice(params: SliceInput):
    """生成固定垂直扫描线（coordinate=x）的 Y–T 切片图并返回。

    每帧取第 x 列作为一行，按时间自上而下堆叠：垂直运动的物体呈斜线。
    横轴是原视频 y。返回 [JSON 元数据, PNG 图像]。
    """
    return _slice_impl(params, "yt")


@mcp.tool(
    name="pixelprobe_save_frame",
    annotations={
        "title": "保存指定帧 PNG",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_save_frame(params: SaveFrameInput) -> str:
    """把原始分辨率帧或裁剪区域保存为 PNG；目标存在时覆盖。"""
    try:
        arr, idx, t, info = _load_frame(params)
        saved = _save_png(arr, params.output_path)
        return _json({
            "saved_path": saved,
            "frame": idx,
            "time_seconds": t,
            "time_ms": seconds_to_ms(t) if t is not None else None,
            "source_width": info.width,
            "source_height": info.height,
            "width": int(arr.shape[1]),
            "height": int(arr.shape[0]),
            "crop": params.crop or None,
        })
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_save_timeline",
    annotations={
        "title": "保存像素时间线 PNG",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_save_timeline(params: SaveTimelineInput) -> str:
    """把原始时间线矩阵保存为 PNG；目标存在时覆盖。"""
    try:
        result = _extract_timeline(params)
        raw = result.matrix
        saved = _save_png(raw, params.output_path)
        return _json({
            "saved_path": saved,
            "k_points": len(result.points),
            "t_frames": len(result.frames),
            "raw_width": int(raw.shape[1]),
            "raw_height": int(raw.shape[0]),
            "sample_type": result.sample_type,
        })
    except PixelProbeError as exc:
        return _error_text(exc)


def _save_slice_impl(params: SaveSliceInput, slice_type: str) -> str:
    try:
        result = _create_slice(params, slice_type)
        raw = result.array
        saved = _save_png(raw, params.output_path)
        return _json({
            "saved_path": saved,
            "slice_type": slice_type,
            "fixed_coordinate": result.fixed_coordinate,
            "t_frames": len(result.frames),
            "raw_width": int(raw.shape[1]),
            "raw_height": int(raw.shape[0]),
        })
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_save_xt_slice",
    annotations={
        "title": "保存 X–T 切片 PNG",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_save_xt_slice(params: SaveSliceInput) -> str:
    """把原始 X–T 切片保存为 PNG；目标存在时覆盖。"""
    return _save_slice_impl(params, "xt")


@mcp.tool(
    name="pixelprobe_save_yt_slice",
    annotations={
        "title": "保存 Y–T 切片 PNG",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_save_yt_slice(params: SaveSliceInput) -> str:
    """把原始 Y–T 切片保存为 PNG；目标存在时覆盖。"""
    return _save_slice_impl(params, "yt")


@mcp.tool(
    name="pixelprobe_detect_changes",
    annotations={
        "title": "定位变化最大的帧",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_detect_changes(params: DetectChangesInput) -> str:
    """计算相邻帧变化量并返回变化最大的 Top N 帧——定位事件时刻的最快方式。

    point（单像素，得分 0~765）/ rect（区域平均绝对差，0~255）/
    grid（区域网格整体聚合，0~255，配 step）三选一。得分并列时帧号小者
    在前。拿到峰值帧号后，用 pixelprobe_extract_frame 查看峰值前后帧确认。

    Returns:
        str: JSON，含 mode、frames_analyzed 与 top 列表
        （每项：frame、previous_frame、time_seconds、time_ms、score、
        normalized_score）。
    """
    try:
        point = parse_point(params.point) if params.point else None
        result = core.detect_changes(
            Path(params.path),
            point=point,
            rect=_parse_rect_opt(params.rect),
            grid=_parse_rect_opt(params.grid),
            step=params.step,
            start_frame=params.start_frame,
            end_frame=params.end_frame,
            start=params.start,
            end=params.end,
            sample_every=params.sample_every,
        )
        top = core.top_changes(result.records, params.top)
        return _json(
            {
                "mode": result.mode,
                "start_frame": result.frame_range.start,
                "end_frame": result.frame_range.end,
                "sample_every": result.frame_range.sample_every,
                "frames_analyzed": result.frames_analyzed,
                "top": [r.to_dict() for r in top],
            }
        )
    except PixelProbeError as exc:
        return _error_text(exc)


def main() -> None:
    """console_scripts 入口：以 stdio 传输运行。"""
    mcp.run()


if __name__ == "__main__":
    main()
