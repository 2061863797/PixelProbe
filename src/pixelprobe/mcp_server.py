"""PixelProbe MCP Server（stdio）。

把 pixelprobe.core 的确定性媒体分析能力包装为 MCP 工具，供 AI Agent 调用。
AI 原有的视觉/视频理解负责判断画面语义；PixelProbe 用于辅助定位候选片段，
并提供精确的帧号、时间戳、坐标与像素数据。

分析工具只读、无副作用，帧图与切片图直接以图像内容返回给模型；
磁盘写入由独立的 save 工具完成。坐标/帧号/时间规范与 CLI 完全一致：
原点左上角、帧号从 0 起、范围为闭区间、时间单位秒。

工具参数一律为扁平参数（字段直接出现在 schema 顶层，无嵌套对象），
共用参数的描述集中定义在下方"参数描述"一节，避免多处漂移。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Annotated, Any, Optional

import numpy as np
from PIL import Image as PILImage
from pydantic import Field

from mcp.server.fastmcp import FastMCP, Image

from pixelprobe import core
from pixelprobe.models.errors import PixelProbeError
from pixelprobe.output.image_writer import fit_within, save_png, scale_nearest
from pixelprobe.output.plot import render_curve
from pixelprobe.utils.coordinates import parse_point, parse_rect
from pixelprobe.utils.timecode import seconds_to_ms

SERVER_INSTRUCTIONS = """
PixelProbe 是 AI 原生视觉/视频理解的精确数据辅助工具，不是其替代品。

工作原则：
1. 画面中的对象、动作、事件、因果和上下文，主要依据你原有的视觉或视频理解能力判断。客户端支持直接查看视频时，优先整体观看并形成语义判断；无法直接读取视频时，使用 extract_frame 提取代表帧，再用你的视觉能力理解画面。
2. PixelProbe 用于读取媒体信息、精确取帧、核对坐标/颜色，以及把视觉判断精确到帧号和时间戳。
3. detect_changes、extract_timeline 和 X-T/Y-T 切片只用于筛选候选时间或发现数值模式。像素变化不等于对象移动，也不能单独证明某个事件发生。
4. 对候选事件至少查看发生前、候选帧和发生后的画面，并与原生视频理解互相验证；不要仅凭变化峰值、时间线或切片图给出语义结论。
5. 问题不确定或目标不明确时，先用视觉理解建立假设，再用这些工具逐步缩小时间和区域；保留仍无法确认的不确定性，不要把数值相关性表述为事实。
6. 用户需要精确数字时，以 PixelProbe 返回的原始分辨率坐标、0 起始帧号、真实时间戳和像素值为准。只有用户明确需要文件时才调用 save 工具。
7. 面对未知视频，优先用 scan_media 一次调用建立概览（信息、代表帧网格、变化事件、异常帧），再逐步聚焦。temporal_reduce、compare_frames、optical_flow 和 spectrum 的输出是数值证据，语义结论仍需用 extract_frame 查看原始帧并以视觉确认。

场景速查（按问题选入口，结论一律回到原始帧视觉确认）：
- 不了解这个视频 → scan_media；只想快速浏览画面 → sample_frames。
- "什么时候发生了变化/事件" → detect_changes（事件分段）→ compare_frames 看具体哪里变了 → extract_frame 看前后帧确认。
- 单帧看不见、疑似藏在噪声/时间维度里的内容（隐藏图案、水印、坏点）→ temporal_reduce（op=std 配 destripe/smooth；运动能量用 op=diff）。
- 怀疑周期性（闪烁、频闪、周期噪声）→ temporal_spectrum；画面条纹/摩尔纹 → spatial_spectrum。
- 运动方向/快慢、镜头运动还是物体运动 → optical_flow（compensate_global=true 扣除镜头运动）。
- 运动轨迹随时间的形态 → xt_slice / yt_slice；固定点颜色随时间变化 → extract_timeline。
- 精确读数（坐标、颜色、区域统计）→ inspect_pixels / analyze_region。
""".strip()

mcp = FastMCP("pixelprobe_mcp", instructions=SERVER_INSTRUCTIONS)

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


# ---------- 参数描述（扁平参数共用定义） ----------

_MediaPath = Annotated[str, Field(
    description="图片或视频文件路径（支持中文路径），例如 C:/videos/input.mp4",
    min_length=1,
)]
_VideoPath = Annotated[str, Field(description="视频路径", min_length=1)]
_FramePath = Annotated[str, Field(description="视频（或图片）路径", min_length=1)]
_FrameNo = Annotated[Optional[int], Field(
    description="帧号（从 0 开始），与 time 二选一", ge=0,
)]
_TimeSec = Annotated[Optional[float], Field(
    description="时间（秒，允许小数），与 frame 二选一", ge=0,
)]
_Crop = Annotated[Optional[str], Field(
    description="裁剪区域 'x,y,width,height'（原始像素坐标），例如 '400,200,300,300'",
)]
_OutputPath = Annotated[str, Field(
    description="PNG 输出路径；目标存在时覆盖", min_length=1,
)]
# 帧范围/时间范围公共字段（闭区间；帧与秒不能混用）
_StartFrame = Annotated[Optional[int], Field(
    description="起始帧（含），与 start/end 秒范围不能混用", ge=0,
)]
_EndFrame = Annotated[Optional[int], Field(description="结束帧（含）", ge=0)]
_StartSec = Annotated[Optional[float], Field(
    description="起始时间（秒），与帧范围不能混用", ge=0,
)]
_EndSec = Annotated[Optional[float], Field(description="结束时间（秒）", ge=0)]
_SampleEvery = Annotated[int, Field(
    description="每隔 N 帧采样一次（长视频建议加大以省时间）", ge=1,
)]
_TimelinePoints = Annotated[Optional[list[str]], Field(
    description="像素坐标列表，每项 'x,y'；与 grid 二选一", max_length=500,
)]
_TimelineGrid = Annotated[Optional[str], Field(
    description="网格采样区域 'x,y,width,height'；与 points 二选一",
)]
_GridStep = Annotated[Optional[int], Field(
    description="网格采样步长（与 grid 搭配）", ge=1,
)]
_BlockSize = Annotated[Optional[int], Field(
    description="像素块边长 N：每个采样位置取 N×N 块平均 RGB（与 grid 搭配）", ge=1,
)]
_SliceCoordinate = Annotated[int, Field(
    description="扫描线坐标：xt 工具填 y（水平线），yt 工具填 x（垂直线）", ge=0,
)]
_MaxDim = Annotated[int, Field(
    description=f"返回图像的最大边长（像素），默认 {DEFAULT_MAX_DIM}；分析像素请用 inspect_pixels，不要靠放大图目测",
    ge=16, le=HARD_MAX_DIM,
)]


def _load_frame(path: str, frame: Optional[int], time: Optional[float],
                crop: Optional[str]):
    return core.get_frame(
        Path(path), frame=frame, time=time, crop=_parse_rect_opt(crop)
    )


def _extract_timeline(path: str, points: Optional[list[str]],
                      grid: Optional[str], step: Optional[int],
                      block_size: Optional[int],
                      start_frame: Optional[int], end_frame: Optional[int],
                      start: Optional[float], end: Optional[float],
                      sample_every: int):
    return core.extract_timelines(
        Path(path),
        points=_parse_points(points),
        grid=_parse_rect_opt(grid),
        step=step,
        block_size=block_size,
        start_frame=start_frame,
        end_frame=end_frame,
        start=start,
        end=end,
        sample_every=sample_every,
    )


def _create_slice(path: str, coordinate: int,
                  start_frame: Optional[int], end_frame: Optional[int],
                  start: Optional[float], end: Optional[float],
                  sample_every: int, slice_type: str):
    fn = core.create_xt_slice if slice_type == "xt" else core.create_yt_slice
    return fn(Path(path), coordinate, start_frame, end_frame,
              start, end, sample_every)


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
def pixelprobe_get_media_info(path: _MediaPath) -> str:
    """读取图片或视频的基本信息，是分析任何媒体的第一步。

    返回 JSON：media_type、width、height、fps、frame_count（可能为估算值，
    见 frame_count_estimated）、duration_seconds、codec、pixel_format、
    is_vfr（可变帧率检测）等。后续所有坐标/帧号参数都应基于这里返回的
    尺寸和帧数范围。

    Returns:
        str: MediaInfo 的 JSON 字符串；失败时返回 "Error[CODE]: ..." 文本。
    """
    try:
        return _json(core.get_media_info(Path(path)).model_dump())
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
def pixelprobe_extract_frame(
    path: _FramePath,
    frame: _FrameNo = None,
    time: _TimeSec = None,
    crop: _Crop = None,
    max_dim: _MaxDim = DEFAULT_MAX_DIM,
):
    """提取视频指定帧（或图片本身）并直接以图像返回，供模型查看。

    用于"只看必要的帧"：先用 pixelprobe_detect_changes / timeline / 切片
    定位重点帧号，再用本工具查看该帧。支持 crop 裁剪聚焦局部。
    返回内容为 [JSON 元数据, PNG 图像]；元数据含 frame、time_seconds、
    原始尺寸与返回图像尺寸。注意：返回图像可能按 max_dim 缩小过，
    精确像素值请用 pixelprobe_inspect_pixels（始终基于原始分辨率）。
    """
    try:
        arr, idx, t, info = _load_frame(path, frame, time, crop)
        preview = fit_within(arr, max_dim, max_dim)
        # 小画面/小裁剪自动最近邻放大，便于模型观察。
        preview, display_scale = _auto_scale(preview)
        preview = fit_within(preview, max_dim, max_dim)
        meta = {
            "frame": idx,
            "time_seconds": t,
            "time_ms": seconds_to_ms(t) if t is not None else None,
            "source_width": info.width,
            "source_height": info.height,
            "crop": crop or None,
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
def pixelprobe_inspect_pixels(
    path: _MediaPath,
    points: Annotated[list[str], Field(
        description="像素坐标列表，每项 'x,y'，例如 ['520,340','600,400']",
        min_length=1, max_length=200,
    )],
    frame: _FrameNo = None,
    time: _TimeSec = None,
) -> str:
    """读取一个或多个像素的精确 RGB/HEX/HSV/亮度（始终基于原始分辨率）。

    视频用 frame 或 time 指定帧（都不给默认第 0 帧）。坐标原点在左上角。
    坐标越界会返回有效范围提示。

    Returns:
        str: JSON，含 frame、time_seconds 及 pixels 列表
        （每项：x,y,pixel_id,rgb{r,g,b},hex,hsv{h:0-360,s:0-100,v:0-100},luminance)。
    """
    try:
        arr, idx, t, _info = core.load_frame(Path(path), frame=frame, time=time)
        samples = core.inspect_pixels(
            arr, _parse_points(points) or [], frame=idx, time_seconds=t
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
def pixelprobe_analyze_region(
    path: _MediaPath,
    rect: Annotated[str, Field(
        description="矩形区域 'x,y,width,height'，例如 '400,200,200,150'",
    )],
    frame: _FrameNo = None,
    time: _TimeSec = None,
) -> str:
    """计算某帧矩形区域的颜色统计（均值/中位数/最值/标准差/HSV/亮度）。

    用于快速了解一块区域"整体是什么颜色、是否均匀"，比逐像素查询省得多。

    Returns:
        str: JSON，含 frame、time_seconds 与 statistics
        （rect、pixel_count、mean_rgb、median_rgb、min_rgb、max_rgb、
        std_rgb、mean_hsv、mean_luminance、std_luminance）。
    """
    try:
        rect_t = parse_rect(rect)
        arr, idx, t, _info = core.load_frame(Path(path), frame=frame, time=time)
        stats = core.analyze_region(arr, rect_t)
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
def pixelprobe_extract_timeline(
    path: _VideoPath,
    points: _TimelinePoints = None,
    grid: _TimelineGrid = None,
    step: _GridStep = None,
    block_size: _BlockSize = None,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    sample_every: _SampleEvery = 1,
    include_values: Annotated[bool, Field(
        description=f"是否在 JSON 中内联返回矩阵数值（仅当 K×T <= {MAX_INLINE_VALUES} 时允许）",
    )] = False,
):
    """提取固定像素随时间的颜色矩阵 [K,T,3]，并以图像返回（横轴=时间，纵轴=像素点）。

    这是辅助视觉理解定位候选时刻的工具，不替代观看视频。图中颜色突变只说明
    采样像素发生变化，不能单独证明对象移动或某个语义事件发生；定位后必须提取
    前后帧并用 AI 的视觉/视频能力确认。points（明确坐标）与 grid（区域网格
    采样，配 step 或 block_size）二选一。视频只解码一次，长视频请用
    sample_every 降采样。
    返回 [JSON 元数据, PNG 时间线图]；元数据的 frames/times 给出每一列
    对应的帧号与秒数（图像可能被整数倍放大，倍数见 display_scale）。
    include_values=true 时内联返回矩阵数值（小规模时用于精确比对）。
    """
    try:
        result = _extract_timeline(
            path, points, grid, step, block_size,
            start_frame, end_frame, start, end, sample_every,
        )
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
        if include_values:
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


def _slice_impl(slice_type: str, path: str, coordinate: int,
                start_frame: Optional[int], end_frame: Optional[int],
                start: Optional[float], end: Optional[float],
                sample_every: int):
    try:
        result = _create_slice(path, coordinate, start_frame, end_frame,
                               start, end, sample_every, slice_type)
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
def pixelprobe_xt_slice(
    path: _VideoPath,
    coordinate: _SliceCoordinate,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    sample_every: _SampleEvery = 1,
):
    """生成固定水平扫描线（coordinate=y）的 X–T 切片图并返回。

    每帧取第 y 行，按时间自上而下堆叠：水平运动的物体呈斜线，
    静止物体呈竖直条带，全画面事件（闪光/切镜头）呈水平横条。
    斜线起点所在行即运动开始的帧（行号 = frames 列表下标）。
    返回 [JSON 元数据, PNG 图像]。
    """
    return _slice_impl("xt", path, coordinate, start_frame, end_frame,
                       start, end, sample_every)


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
def pixelprobe_yt_slice(
    path: _VideoPath,
    coordinate: _SliceCoordinate,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    sample_every: _SampleEvery = 1,
):
    """生成固定垂直扫描线（coordinate=x）的 Y–T 切片图并返回。

    每帧取第 x 列作为一行，按时间自上而下堆叠：垂直运动的物体呈斜线。
    横轴是原视频 y。返回 [JSON 元数据, PNG 图像]。
    """
    return _slice_impl("yt", path, coordinate, start_frame, end_frame,
                       start, end, sample_every)


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
def pixelprobe_save_frame(
    path: _FramePath,
    output_path: _OutputPath,
    frame: _FrameNo = None,
    time: _TimeSec = None,
    crop: _Crop = None,
) -> str:
    """把原始分辨率帧或裁剪区域保存为 PNG；目标存在时覆盖。"""
    try:
        arr, idx, t, info = _load_frame(path, frame, time, crop)
        saved = _save_png(arr, output_path)
        return _json({
            "saved_path": saved,
            "frame": idx,
            "time_seconds": t,
            "time_ms": seconds_to_ms(t) if t is not None else None,
            "source_width": info.width,
            "source_height": info.height,
            "width": int(arr.shape[1]),
            "height": int(arr.shape[0]),
            "crop": crop or None,
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
def pixelprobe_save_timeline(
    path: _VideoPath,
    output_path: _OutputPath,
    points: _TimelinePoints = None,
    grid: _TimelineGrid = None,
    step: _GridStep = None,
    block_size: _BlockSize = None,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    sample_every: _SampleEvery = 1,
) -> str:
    """把原始时间线矩阵保存为 PNG；目标存在时覆盖。"""
    try:
        result = _extract_timeline(
            path, points, grid, step, block_size,
            start_frame, end_frame, start, end, sample_every,
        )
        raw = result.matrix
        saved = _save_png(raw, output_path)
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


def _save_slice_impl(slice_type: str, path: str, coordinate: int,
                     output_path: str,
                     start_frame: Optional[int], end_frame: Optional[int],
                     start: Optional[float], end: Optional[float],
                     sample_every: int) -> str:
    try:
        result = _create_slice(path, coordinate, start_frame, end_frame,
                               start, end, sample_every, slice_type)
        raw = result.array
        saved = _save_png(raw, output_path)
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
def pixelprobe_save_xt_slice(
    path: _VideoPath,
    coordinate: _SliceCoordinate,
    output_path: _OutputPath,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    sample_every: _SampleEvery = 1,
) -> str:
    """把原始 X–T 切片保存为 PNG；目标存在时覆盖。"""
    return _save_slice_impl("xt", path, coordinate, output_path,
                            start_frame, end_frame, start, end, sample_every)


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
def pixelprobe_save_yt_slice(
    path: _VideoPath,
    coordinate: _SliceCoordinate,
    output_path: _OutputPath,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    sample_every: _SampleEvery = 1,
) -> str:
    """把原始 Y–T 切片保存为 PNG；目标存在时覆盖。"""
    return _save_slice_impl("yt", path, coordinate, output_path,
                            start_frame, end_frame, start, end, sample_every)


@mcp.tool(
    name="pixelprobe_detect_changes",
    annotations={
        "title": "定位候选变化帧",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_detect_changes(
    path: _VideoPath,
    point: Annotated[Optional[str], Field(
        description="单像素坐标 'x,y'",
    )] = None,
    rect: Annotated[Optional[str], Field(
        description="矩形区域 'x,y,width,height'",
    )] = None,
    grid: Annotated[Optional[str], Field(
        description="网格采样区域 'x,y,width,height'（配合 step）",
    )] = None,
    step: _GridStep = None,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    sample_every: _SampleEvery = 1,
    top: Annotated[int, Field(
        description="返回变化最大的前 N 帧", ge=1, le=100,
    )] = 10,
    event_threshold: Annotated[Optional[float], Field(
        description="事件分段阈值（作用于归一化得分；缺省自动取 mean + 3*std）",
        ge=0,
    )] = None,
    include_curve: Annotated[bool, Field(
        description=f"是否在 JSON 中内联完整变化曲线 [[frame, normalized_score], ...]"
                    f"（超过 {MAX_INLINE_VALUES} 条时等距抽稀并标注）",
    )] = False,
    include_curve_image: Annotated[bool, Field(
        description="是否额外返回变化曲线 PNG（事件区间描色）",
    )] = False,
):
    """计算相邻帧变化量：Top N 候选帧 + 事件区间，辅助筛选值得视觉检查的时刻。

    point（单像素，得分 0~765）/ rect（区域平均绝对差，0~255）/
    grid（区域网格整体聚合，0~255，配 step）最多指定一个，
    都不给时默认整帧（full 模式，适合"先概览再聚焦"）。得分并列时帧号
    小者在前。分数只表示像素变化幅度，不能单独证明对象、动作或事件。
    拿到峰值后，必须用 pixelprobe_extract_frame 查看前一帧、候选帧和
    后一帧，并结合 AI 原有的视觉/视频理解确认语义。

    返回 JSON，含 mode、frames_analyzed、top 列表、events 事件区间列表
    （连续超阈记录合并）与 event_threshold_used；include_curve_image=true
    时额外返回曲线 PNG。
    """
    try:
        point_t = parse_point(point) if point else None
        result = core.detect_changes(
            Path(path),
            point=point_t,
            rect=_parse_rect_opt(rect),
            grid=_parse_rect_opt(grid),
            step=step,
            start_frame=start_frame,
            end_frame=end_frame,
            start=start,
            end=end,
            sample_every=sample_every,
        )
        top_records = core.top_changes(result.records, top)
        events, threshold_used = core.segment_events(
            result.records, threshold=event_threshold
        )
        meta: dict[str, Any] = {
            "mode": result.mode,
            "start_frame": result.frame_range.start,
            "end_frame": result.frame_range.end,
            "sample_every": result.frame_range.sample_every,
            "frames_analyzed": result.frames_analyzed,
            "top": [r.to_dict() for r in top_records],
            "events": [e.to_dict() for e in events],
            "event_threshold_used": threshold_used,
        }
        if include_curve:
            curve = [
                [r.frame, r.normalized_score] for r in result.records
            ]
            if len(curve) > MAX_INLINE_VALUES:
                idxs = np.linspace(
                    0, len(curve) - 1, num=MAX_INLINE_VALUES
                ).astype(int)
                curve = [curve[i] for i in dict.fromkeys(idxs.tolist())]
                meta["curve_downsampled"] = True
            meta["curve"] = curve
        if include_curve_image:
            frame_of = {r.frame: i for i, r in enumerate(result.records)}
            prev_of = {
                r.previous_frame: i for i, r in enumerate(result.records)
            }
            spans = [
                (prev_of[e.start_frame], frame_of[e.end_frame])
                for e in events
                if e.start_frame in prev_of and e.end_frame in frame_of
            ]
            image = render_curve(
                [r.normalized_score for r in result.records],
                spans=spans, y_min=0.0,
            )
            meta["curve_image_axis"] = "横轴=记录序（左早右晚），纵轴=归一化得分"
            return [_json(meta), _png_image(image)]
        return _json(meta)
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_temporal_reduce",
    annotations={
        "title": "时间域合成统计图",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_temporal_reduce(
    path: _VideoPath,
    op: Annotated[str, Field(
        description="统计量：mean/median/min/max/std/diff"
                    "（std=时间标准差，找隐藏静态图案/噪声差异；"
                    "diff=相邻帧差均值，即运动能量分布）",
    )] = "std",
    rect: Annotated[Optional[str], Field(
        description="只统计子区域 'x,y,width,height'（也是 median 控内存的手段）",
    )] = None,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    sample_every: _SampleEvery = 1,
    p_low: Annotated[float, Field(
        description="对比度拉伸低百分位（0-100）", ge=0, le=100,
    )] = 1.0,
    p_high: Annotated[float, Field(
        description="对比度拉伸高百分位（0-100）", ge=0, le=100,
    )] = 99.0,
    destripe: Annotated[bool, Field(
        description="扣除逐列/逐行均值，抑制噪声生成或传感器带来的条纹伪影"
                    "（噪声藏图场景建议开启）",
    )] = False,
    smooth: Annotated[int, Field(
        description="N×N 邻域均值平滑（>=2 生效），压制噪声粒度、凸显区域结构"
                    "（噪声藏图场景建议 5~9）",
        ge=0, le=64,
    )] = 0,
    max_dim: _MaxDim = DEFAULT_MAX_DIM,
):
    """把整段视频折叠成一张逐像素时间统计图并返回，揭示单帧看不见的内容。

    典型用途：噪声里的隐藏静态图案 / 慢变水印（op=std + destripe=true，
    图案区域时间方差异常）、运动发生在画面哪里（op=diff）、坏点与固定
    叠加物（op=min/max）。除 median 外全部流式计算，视频只解码一遍；
    长视频用 sample_every 降采样。
    统计图是数值证据，语义结论仍需用 extract_frame 查看原始帧确认。
    返回 [JSON 元数据, PNG 统计图]；元数据含拉伸前原始统计摘要与拉伸端点，
    精确数值判断以元数据为准，不要靠图目测。
    """
    try:
        result = core.temporal_reduce(
            Path(path),
            op=op,  # type: ignore[arg-type]
            rect=_parse_rect_opt(rect),
            start_frame=start_frame,
            end_frame=end_frame,
            start=start,
            end=end,
            sample_every=sample_every,
            p_low=p_low,
            p_high=p_high,
            destripe=destripe,
            smooth=smooth,
        )
        preview = fit_within(result.image, max_dim, max_dim)
        preview, display_scale = _auto_scale(preview)
        meta = {
            "op": result.op,
            "rect": rect or None,
            "start_frame": result.frame_range.start,
            "end_frame": result.frame_range.end,
            "sample_every": result.frame_range.sample_every,
            "frames_analyzed": result.frames_analyzed,
            "stat_min": result.stat_min,
            "stat_max": result.stat_max,
            "stat_mean": result.stat_mean,
            "stretch_low_value": result.stretch_low_value,
            "stretch_high_value": result.stretch_high_value,
            "p_low": result.p_low,
            "p_high": result.p_high,
            "destripe": result.destripe,
            "smooth": result.smooth,
            "raw_width": int(result.image.shape[1]),
            "raw_height": int(result.image.shape[0]),
            "returned_width": int(preview.shape[1]),
            "returned_height": int(preview.shape[0]),
            "display_scale": display_scale,
            "note": "亮=统计值高，暗=统计值低（已按 p_low/p_high 百分位拉伸）",
        }
        return [_json(meta), _png_image(preview)]
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_save_temporal_reduce",
    annotations={
        "title": "保存时间域统计图 PNG",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_save_temporal_reduce(
    path: _VideoPath,
    output_path: _OutputPath,
    op: Annotated[str, Field(
        description="统计量：mean/median/min/max/std/diff",
    )] = "std",
    rect: Annotated[Optional[str], Field(
        description="只统计子区域 'x,y,width,height'",
    )] = None,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    sample_every: _SampleEvery = 1,
    p_low: Annotated[float, Field(
        description="对比度拉伸低百分位（0-100）", ge=0, le=100,
    )] = 1.0,
    p_high: Annotated[float, Field(
        description="对比度拉伸高百分位（0-100）", ge=0, le=100,
    )] = 99.0,
    destripe: Annotated[bool, Field(
        description="扣除逐列/逐行均值，抑制条纹伪影",
    )] = False,
    smooth: Annotated[int, Field(
        description="N×N 邻域均值平滑（>=2 生效）", ge=0, le=64,
    )] = 0,
) -> str:
    """把原分辨率时间统计图保存为 PNG；目标存在时覆盖。"""
    try:
        result = core.temporal_reduce(
            Path(path),
            op=op,  # type: ignore[arg-type]
            rect=_parse_rect_opt(rect),
            start_frame=start_frame,
            end_frame=end_frame,
            start=start,
            end=end,
            sample_every=sample_every,
            p_low=p_low,
            p_high=p_high,
            destripe=destripe,
            smooth=smooth,
        )
        saved = _save_png(result.image, output_path)
        return _json({
            "saved_path": saved,
            "op": result.op,
            "frames_analyzed": result.frames_analyzed,
            "stretch_low_value": result.stretch_low_value,
            "stretch_high_value": result.stretch_high_value,
            "destripe": result.destripe,
            "smooth": result.smooth,
            "raw_width": int(result.image.shape[1]),
            "raw_height": int(result.image.shape[0]),
        })
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_compare_frames",
    annotations={
        "title": "两帧差异比较",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_compare_frames(
    path: _VideoPath,
    frame_a: Annotated[Optional[int], Field(
        description="帧 A 帧号（从 0 开始），与 time_a 二选一", ge=0,
    )] = None,
    time_a: Annotated[Optional[float], Field(
        description="帧 A 时间（秒），与 frame_a 二选一", ge=0,
    )] = None,
    frame_b: Annotated[Optional[int], Field(
        description="帧 B 帧号（从 0 开始），与 time_b 二选一", ge=0,
    )] = None,
    time_b: Annotated[Optional[float], Field(
        description="帧 B 时间（秒），与 frame_b 二选一", ge=0,
    )] = None,
    rect: Annotated[Optional[str], Field(
        description="只比较子区域 'x,y,width,height'",
    )] = None,
    threshold: Annotated[int, Field(
        description="变化像素判定阈值（每像素三通道最大差，0-255）",
        ge=0, le=255,
    )] = 10,
    colormap: Annotated[str, Field(
        description="差异图伪彩方案：gray/fire",
    )] = "fire",
    max_dim: _MaxDim = DEFAULT_MAX_DIM,
):
    """比较视频中任意两帧，回答"具体哪里变了、变了多少"。

    detect_changes 找到候选帧后，用本工具比较峰值前后帧，把变化定位到
    具体区域。返回 [JSON, 差异热力图 PNG]；JSON 含 mean_abs_diff、
    max_abs_diff、changed_pixels/changed_ratio 与超阈值像素外接 bbox
    （原始分辨率坐标）。热力图按最大差拉伸，精确数值以 JSON 为准。
    差异只说明像素不同，是否是"同一对象移动"仍需查看原始帧确认。
    """
    try:
        result = core.compare_frames(
            Path(path),
            frame_a=frame_a,
            time_a=time_a,
            frame_b=frame_b,
            time_b=time_b,
            rect=_parse_rect_opt(rect),
            threshold=threshold,
            colormap=colormap,  # type: ignore[arg-type]
        )
        preview = fit_within(result.diff_image, max_dim, max_dim)
        preview, display_scale = _auto_scale(preview)
        meta = {
            "frame_a": result.frame_a,
            "frame_b": result.frame_b,
            "time_a": result.time_a,
            "time_b": result.time_b,
            "rect": rect or None,
            "threshold": result.threshold,
            "mean_abs_diff": result.mean_abs_diff,
            "max_abs_diff": result.max_abs_diff,
            "changed_pixels": result.changed_pixels,
            "changed_ratio": result.changed_ratio,
            "bbox": (
                {"x": result.bbox[0], "y": result.bbox[1],
                 "width": result.bbox[2], "height": result.bbox[3]}
                if result.bbox else None
            ),
            "source_width": result.width,
            "source_height": result.height,
            "returned_width": int(preview.shape[1]),
            "returned_height": int(preview.shape[0]),
            "display_scale": display_scale,
        }
        return [_json(meta), _png_image(preview)]
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_sample_frames",
    annotations={
        "title": "采样帧网格概览",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_sample_frames(
    path: _VideoPath,
    count: Annotated[int, Field(
        description="等距抽取的帧数", ge=1, le=64,
    )] = 9,
    cols: Annotated[Optional[int], Field(
        description="网格列数（缺省自动取近似方形）", ge=1,
    )] = None,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    tile_max_dim: Annotated[int, Field(
        description="单格最大边长（像素）", ge=32, le=768,
    )] = 320,
    annotate: Annotated[bool, Field(
        description="是否在每格下方绘制帧号/秒标注条",
    )] = True,
):
    """等距抽 N 帧拼成一张带标注的网格图，一次调用建立整段视频的概览。

    这是"无法直接观看视频时了解内容"的第一步：先用本工具看全貌，
    再用 detect_changes / extract_frame 聚焦具体时刻。
    返回 [JSON 元数据, PNG 网格图]；元数据的 frames/times 与网格内
    从左到右、从上到下一一对应。
    """
    try:
        result = core.sample_frames(
            Path(path),
            count=count,
            cols=cols,
            start_frame=start_frame,
            end_frame=end_frame,
            start=start,
            end=end,
            tile_max_dim=tile_max_dim,
            annotate=annotate,
        )
        preview = fit_within(result.image, HARD_MAX_DIM, HARD_MAX_DIM)
        meta = {
            "frames": result.frames,
            "times": result.times,
            "cols": result.cols,
            "rows": result.rows,
            "tile_width": result.tile_width,
            "tile_height": result.tile_height,
            "raw_width": int(result.image.shape[1]),
            "raw_height": int(result.image.shape[0]),
            "returned_width": int(preview.shape[1]),
            "returned_height": int(preview.shape[0]),
            "axis": "从左到右、从上到下按 frames 顺序排列，每格下方标注 f=帧号 t=秒",
        }
        return [_json(meta), _png_image(preview)]
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_scan_media",
    annotations={
        "title": "一键媒体概览扫描",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_scan_media(
    path: _VideoPath,
    sheet_count: Annotated[int, Field(
        description="概览网格抽帧数", ge=1, le=64,
    )] = 9,
    sample_every: Annotated[Optional[int], Field(
        description="每隔 N 帧采样一次（缺省自动：全片约 1800 帧封顶）", ge=1,
    )] = None,
    event_threshold: Annotated[Optional[float], Field(
        description="事件分段阈值（缺省自动 mean + 3*std）", ge=0,
    )] = None,
):
    """未知视频的第一步概览工具：一次调用产出信息 + 代表帧网格 + 变化事件 + 异常帧。

    内部单遍解码同时完成整帧变化曲线、等距抽帧和亮度异常检测（黑帧/白帧/
    纯色帧/闪帧），比分别调用 get_media_info + sample_frames + detect_changes
    快得多。返回 [JSON 摘要, 网格图 PNG, 变化曲线 PNG]。
    概览只是候选证据：对感兴趣的事件仍需 extract_frame 查看前后帧确认语义。

    扫描后按发现选择下一步：有事件 → compare_frames 定位变化区域再
    extract_frame 确认；画面疑似纯噪声/单帧无内容 → temporal_reduce(op=std,
    destripe=true) 找隐藏结构；疑似周期闪烁 → temporal_spectrum；
    需要运动方向/区分镜头运动 → optical_flow。
    """
    try:
        result = core.scan_media(
            Path(path),
            sheet_count=sheet_count,
            sample_every=sample_every,
            event_threshold=event_threshold,
        )
        sheet_preview = fit_within(result.sheet.image, HARD_MAX_DIM, HARD_MAX_DIM)
        meta: dict[str, Any] = {
            "info": result.info.model_dump(),
            "effective_sample_every": result.effective_sample_every,
            "frames_analyzed": result.frames_analyzed,
            "sheet_frames": result.sheet.frames,
            "sheet_times": result.sheet.times,
            "sheet_cols": result.sheet.cols,
            "sheet_rows": result.sheet.rows,
            "events": [e.to_dict() for e in result.events],
            "event_threshold_used": result.event_threshold,
            "anomalies": result.anomalies,
            "anomalies_truncated": result.anomalies_truncated,
        }
        # 注意：必须先写完 meta 再序列化（_json 会立即 dumps）
        if result.records:
            curve_image = render_curve(
                [r.normalized_score for r in result.records], y_min=0.0
            )
            meta["images"] = (
                "第 1 张=代表帧网格（每格标注 f=帧号 t=秒），"
                "第 2 张=整帧变化曲线（横轴=时间）"
            )
            return [_json(meta), _png_image(sheet_preview),
                    _png_image(curve_image)]
        meta["images"] = "仅代表帧网格（帧数不足两帧，无变化曲线）"
        return [_json(meta), _png_image(sheet_preview)]
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_temporal_spectrum",
    annotations={
        "title": "时间域周期检测",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_temporal_spectrum(
    path: _VideoPath,
    source: Annotated[str, Field(
        description="分析序列：luma（区域平均亮度）/ change（相邻帧变化量）",
    )] = "luma",
    rect: Annotated[Optional[str], Field(
        description="只统计子区域 'x,y,width,height'；与 point 最多给一个",
    )] = None,
    point: Annotated[Optional[str], Field(
        description="只统计单像素 'x,y'；与 rect 最多给一个",
    )] = None,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    sample_every: _SampleEvery = 1,
):
    """检测周期闪烁/周期变化：对亮度或变化序列做 FFT，报告主频与周期。

    用于判断"噪声是否周期生成、某区域是否固定频率闪烁、是否存在编码或
    显示器刷新干扰"。返回 [JSON, 谱线图 PNG]；主频为 None 表示序列平坦。
    频率相关性不等于语义结论；vfr_warning=true 时频率按平均帧率换算，仅供参考。
    """
    try:
        result = core.temporal_spectrum(
            Path(path),
            source=source,  # type: ignore[arg-type]
            rect=_parse_rect_opt(rect),
            point=parse_point(point) if point else None,
            start_frame=start_frame,
            end_frame=end_frame,
            start=start,
            end=end,
            sample_every=sample_every,
        )
        meta = {
            "source": result.source,
            "samples": result.samples,
            "effective_fps": result.effective_fps,
            "dominant_freq_hz": result.dominant_freq_hz,
            "period_seconds": result.period_seconds,
            "period_frames": result.period_frames,
            "peak_ratio": result.peak_ratio,
            "top_peaks": result.top_peaks,
            "vfr_warning": result.vfr_warning,
            "axis": "谱线图横轴=频率（左低右高，不含直流），纵轴=幅度；竖线=主频",
        }
        return [_json(meta), _png_image(result.spectrum_image)]
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_spatial_spectrum",
    annotations={
        "title": "单帧空间频谱",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_spatial_spectrum(
    path: _MediaPath,
    frame: _FrameNo = None,
    time: _TimeSec = None,
    rect: Annotated[Optional[str], Field(
        description="只分析子区域 'x,y,width,height'",
    )] = None,
    max_dim: _MaxDim = DEFAULT_MAX_DIM,
):
    """对单帧灰度做二维 FFT，检测条纹/摩尔纹/重复纹理。

    返回 [JSON, 中心化 log 幅度谱图 PNG]；peaks 给出显著周期成分的
    周期（像素）与频率向量方向（条纹走向与其垂直）。
    """
    try:
        result = core.spatial_spectrum(
            Path(path), frame=frame, time=time, rect=_parse_rect_opt(rect)
        )
        preview = fit_within(result.spectrum_image, max_dim, max_dim)
        preview, display_scale = _auto_scale(preview)
        meta = {
            "frame": result.frame,
            "time_seconds": result.time_seconds,
            "rect": rect or None,
            "width": result.width,
            "height": result.height,
            "peaks": result.peaks,
            "display_scale": display_scale,
            "axis": "谱图中心=零频，亮点离中心的距离=频率，方向=频率向量方向",
        }
        return [_json(meta), _png_image(preview)]
    except PixelProbeError as exc:
        return _error_text(exc)


@mcp.tool(
    name="pixelprobe_optical_flow",
    annotations={
        "title": "稠密光流分析",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def pixelprobe_optical_flow(
    path: _VideoPath,
    frame_a: Annotated[Optional[int], Field(
        description="帧 A 帧号（两帧模式），与 time_a 二选一", ge=0,
    )] = None,
    time_a: Annotated[Optional[float], Field(
        description="帧 A 时间（秒），与 frame_a 二选一", ge=0,
    )] = None,
    frame_b: Annotated[Optional[int], Field(
        description="帧 B 帧号（两帧模式），与 time_b 二选一", ge=0,
    )] = None,
    time_b: Annotated[Optional[float], Field(
        description="帧 B 时间（秒），与 frame_b 二选一", ge=0,
    )] = None,
    accumulate: Annotated[bool, Field(
        description="累积模式：对帧范围逐对光流相加（改用 start_frame 等范围参数）",
    )] = False,
    start_frame: _StartFrame = None,
    end_frame: _EndFrame = None,
    start: _StartSec = None,
    end: _EndSec = None,
    sample_every: _SampleEvery = 1,
    compensate_global: Annotated[bool, Field(
        description="估计并扣除全局仿射运动（镜头平移/旋转/缩放），"
                    "用于区分镜头运动与物体运动",
    )] = False,
    mag_threshold: Annotated[float, Field(
        description="运动区域判定的位移阈值（像素）", ge=0,
    )] = 1.0,
    max_dim: _MaxDim = DEFAULT_MAX_DIM,
):
    """稠密光流（Farneback）：运动方向/幅度分布、全局运动、运动区域 bbox。

    需要可选依赖：pip install "pixelprobe[flow]"。
    适用：运动伪装素材、镜头抖动/移动区分、慢速移动目标定位。
    返回 [JSON, 方向着色流场图, 幅度伪彩图]；流场图 hue=方向、亮度=幅度。
    光流表示像素位移，不等于对象身份；结论仍需查看原始帧确认。
    """
    try:
        result = core.compute_flow(
            Path(path),
            frame_a=frame_a,
            time_a=time_a,
            frame_b=frame_b,
            time_b=time_b,
            start_frame=start_frame,
            end_frame=end_frame,
            start=start,
            end=end,
            sample_every=sample_every,
            accumulate=accumulate,
            compensate_global=compensate_global,
            mag_threshold=mag_threshold,
        )
        flow_preview = fit_within(result.flow_image, max_dim, max_dim)
        flow_preview, display_scale = _auto_scale(flow_preview)
        mag_preview = fit_within(result.magnitude_image, max_dim, max_dim)
        mag_preview, _ = _auto_scale(mag_preview)
        meta = {
            "frame_a": result.frame_a,
            "frame_b": result.frame_b,
            "accumulated": result.accumulated,
            "frames_analyzed": result.frames_analyzed,
            "mean_magnitude": result.mean_magnitude,
            "max_magnitude": result.max_magnitude,
            "p95_magnitude": result.p95_magnitude,
            "dominant_angle_deg": result.dominant_angle_deg,
            "angle_convention": "0°=向右，y 向下为正方向（逆时针为负）",
            "global_motion": result.global_motion,
            "compensated": result.compensated,
            "motion_bbox": (
                {"x": result.motion_bbox[0], "y": result.motion_bbox[1],
                 "width": result.motion_bbox[2],
                 "height": result.motion_bbox[3]}
                if result.motion_bbox else None
            ),
            "mag_threshold": result.mag_threshold,
            "display_scale": display_scale,
            "images": "第 1 张=方向着色流场（hue=方向，亮=快），第 2 张=幅度伪彩图",
        }
        return [_json(meta), _png_image(flow_preview), _png_image(mag_preview)]
    except PixelProbeError as exc:
        return _error_text(exc)


def main() -> None:
    """console_scripts 入口：以 stdio 传输运行。"""
    mcp.run()


if __name__ == "__main__":
    main()
