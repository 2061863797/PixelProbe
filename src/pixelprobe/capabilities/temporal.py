"""时间序列类能力：timeline、xt、yt、changes、reduce、compare、sheet、scan、
spectrum、spectrum2d、flow。计算统一经 core 或引擎（request_builders + generate）。"""

from __future__ import annotations

from typing import Literal, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

import pixelprobe
from pixelprobe import core
from pixelprobe.address import (
    Address,
    TimeSpec,
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
from pixelprobe.capabilities.base import (
    AddressRule,
    Capability,
    Invocation,
    Outcome,
    register,
)
from pixelprobe.core.frame_selector import FrameRange
from pixelprobe.core.timeline_extractor import TimelineResult
from pixelprobe.models.errors import AddressError
from pixelprobe.models.pixel import PixelCoordinate
from pixelprobe.output.plot import render_curve
from pixelprobe.request_builders import (
    flow_request,
    reduce_request,
    spacetime_request,
    spatial_spectrum_request,
    temporal_spectrum_request,
    timeline_matrix,
    timeline_request,
)
from pixelprobe.utils.coordinates import pixel_id_from_xy

_SINGLE = frozenset({"single"})
_RANGE = frozenset({"single", "range", "all"})
_SPAN = frozenset({"range", "all"})
_PAIR = frozenset({"list"})


def _size(call: Invocation) -> tuple[int, int]:
    if not needs_media_size(call.address):
        return 0, 0
    info = core.get_media_info(call.media)
    return info.width, info.height


def _rect_dict(rect) -> dict | None:
    if rect is None:
        return None
    return {"x": rect[0], "y": rect[1], "width": rect[2], "height": rect[3]}


def _progress(call: Invocation) -> None:
    if call.progress is not None:
        call.progress(1, 1)


def _event_refs(events, prefix: str = "event") -> dict[str, str]:
    """事件编号 → 覆盖该事件的帧范围地址（含变化前最后一帧）。"""
    return {
        f"{prefix}{number}": str(Address(time=_frame_span(event.start_frame, event.end_frame)))
        for number, event in enumerate(events, start=1)
    }


def _frame_span(first: int, last: int) -> TimeSpec:
    if first == last:
        return TimeSpec("frame", "single", (first,))
    return TimeSpec("frame", "range", (first, last))


# ---------------- timeline ----------------

class TimelineOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_size: Optional[int] = Field(
        default=None, ge=1, description="像素块边长 N：每个采样位置取 N×N 块平均（与 grid 搭配）",
    )
    sort: Literal["selection", "pixel-id", "yx", "xy"] = Field(
        default="selection", description="像素行排序",
    )


def _timeline_request(call: Invocation):
    options: TimelineOptions = call.options  # type: ignore[assignment]
    grid = grid_of(call.address)
    return timeline_request(
        call.media,
        points=points_of(call.address, *_size(call)) or None,
        grid=(grid.x, grid.y, grid.width, grid.height) if grid else None,
        step=grid.step if grid else None,
        block_size=options.block_size,
        **range_options(call.address),
        sort=options.sort,
    )


def _timeline(call: Invocation) -> Outcome:
    options: TimelineOptions = call.options  # type: ignore[assignment]
    request, ordered, width, height = _timeline_request(call)
    tensor = pixelprobe.generate(request).request_tensors[0][0]
    _progress(call)
    matrix = timeline_matrix(tensor)
    if options.block_size is not None:
        matrix = np.round(matrix).astype(np.uint8)
    frames = list(tensor.attributes["presentation_indices"])
    times = list(tensor.attributes["timeline_timestamps_seconds"])
    result = TimelineResult(
        matrix=matrix,
        points=[
            PixelCoordinate(x=x, y=y, pixel_id=pixel_id_from_xy(x, y, width))
            for x, y in ordered
        ],
        frames=frames,
        times=times,
        frame_range=FrameRange(frames[0], frames[-1], request.selection.sample_every),
        sample_type="point" if options.block_size is None else "block_mean",
        block_size=options.block_size,
        sort=options.sort,
        width=width,
        height=height,
        tensor=tensor,
    )
    return Outcome(
        data={
            "points": [point.model_dump() for point in result.points],
            "start_frame": result.frame_range.start,
            "end_frame": result.frame_range.end,
            "frame_count": len(frames),
            "sample_every": result.frame_range.sample_every,
            "sort": options.sort,
            "sample_type": result.sample_type,
            "block_size": options.block_size,
            "frames": frames,
            "times": times,
        },
        images={"timeline": lambda: matrix},
        artifacts={"timeline": result},
    )


# ---------------- xt / yt ----------------

def _slice_request(call: Invocation, kind: str):
    fixed = row_of(call.address) if kind == "xt" else col_of(call.address)
    if fixed is None:
        example = "row(120)" if kind == "xt" else "col(40)"
        raise AddressError(f'{kind} 需要扫描线位置，例如 "t=1s..3s {example}"')
    return spacetime_request(call.media, kind, fixed, **range_options(call.address)), fixed


def _slice(kind: str):
    def run(call: Invocation) -> Outcome:
        request, fixed = _slice_request(call, kind)
        tensor = pixelprobe.generate(request).request_tensors[0][0]
        _progress(call)
        array = tensor.data.materialize()
        frames = list(tensor.attributes["presentation_indices"])
        return Outcome(
            data={
                "slice_type": kind,
                "fixed_coordinate": fixed,
                "start_frame": frames[0],
                "end_frame": frames[-1],
                "frame_count": len(frames),
                "sample_every": request.selection.sample_every,
                "space_axis": "original_x" if kind == "xt" else "original_y",
                "time_axis": "vertical",
                "frames": frames,
                "times": list(tensor.attributes["timeline_timestamps_seconds"]),
            },
            images={"slice": lambda: array},
        )
    return run


# ---------------- changes ----------------

class ChangesOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    top: int = Field(default=10, ge=1, description="返回变化最大的前 N 帧")
    threshold: Optional[float] = Field(
        default=None, description="事件分段阈值（作用于归一化得分；缺省自动）",
    )


def _changes(call: Invocation) -> Outcome:
    options: ChangesOptions = call.options  # type: ignore[assignment]
    width, height = _size(call)
    points = points_of(call.address, width, height)
    if len(points) > 1:
        raise AddressError("changes 一次只能分析一个点；多个点请用 timeline")
    point = points[0] if points else None
    rect = rect_of(call.address, width, height)
    grid = grid_of(call.address)
    grid_rect = (grid.x, grid.y, grid.width, grid.height) if grid else None
    step = grid.step if grid else None
    full_frame = point is None and rect is None and grid_rect is None
    result = core.detect_changes(
        call.media, point=point, rect=rect, grid=grid_rect, step=step,
        **range_options(call.address), progress=call.progress,
        index=call.index if full_frame and (call.address.step or 1) == 1 else None,
    )
    top_records = core.top_changes(result.records, options.top)
    events, threshold_used = core.segment_events(result.records, threshold=options.threshold)

    def curve() -> np.ndarray:
        frame_of = {r.frame: i for i, r in enumerate(result.records)}
        prev_of = {r.previous_frame: i for i, r in enumerate(result.records)}
        spans = [
            (prev_of[e.start_frame], frame_of[e.end_frame])
            for e in events
            if e.start_frame in prev_of and e.end_frame in frame_of
        ]
        return render_curve(
            [r.normalized_score for r in result.records], spans=spans, y_min=0.0,
        )

    return Outcome(
        data={
            "mode": result.mode,
            "point": {"x": point[0], "y": point[1]} if point else None,
            "rect": _rect_dict(rect),
            "grid": (
                {**_rect_dict(grid_rect), "step": step if step is not None else 1}
                if grid_rect else None
            ),
            "start_frame": result.frame_range.start,
            "end_frame": result.frame_range.end,
            "sample_every": result.frame_range.sample_every,
            "frames_analyzed": result.frames_analyzed,
            "top": [r.to_dict() for r in top_records],
            "events": [e.to_dict() for e in events],
            "event_threshold_used": threshold_used,
            "computed_from": result.computed_from,
        },
        images={"curve": curve},
        artifacts={"records": result.records, "events": events},
        refs=_event_refs(events),
    )


# ---------------- reduce ----------------

class ReduceOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: str = Field(default="std", description="mean/median/min/max/std/diff（diff=相邻帧差均值）")
    p_low: float = Field(default=1.0, description="对比度拉伸低百分位（0-100）")
    p_high: float = Field(default=99.0, description="对比度拉伸高百分位（0-100）")
    destripe: bool = Field(default=False, description="扣除逐列/逐行均值，抑制条纹伪影（只影响预览）")
    smooth: int = Field(default=0, description="N×N 邻域均值平滑（>=2 生效，只影响预览）")


def _reduce_request(call: Invocation):
    options: ReduceOptions = call.options  # type: ignore[assignment]
    rect = rect_of(call.address, *_size(call))
    return reduce_request(
        call.media, operation=options.op, rect=rect, **range_options(call.address),
        p_low=options.p_low, p_high=options.p_high,
        destripe=options.destripe, smooth=options.smooth,
    ), rect


def _reduce(call: Invocation) -> Outcome:
    options: ReduceOptions = call.options  # type: ignore[assignment]
    request, rect = _reduce_request(call)
    data_tensor, preview_tensor = pixelprobe.generate(request).request_tensors[0]
    _progress(call)
    statistic = data_tensor.data.materialize()
    image = preview_tensor.data.materialize()
    frames = tuple(data_tensor.attributes["presentation_indices"])
    attributes = preview_tensor.attributes
    return Outcome(
        data={
            "op": options.op,
            "rect": _rect_dict(rect),
            "start_frame": frames[0],
            "end_frame": frames[-1],
            "sample_every": request.selection.sample_every,
            "frames_analyzed": len(frames),
            "stat_min": [round(float(v), 4) for v in statistic.min(axis=(0, 1))],
            "stat_max": [round(float(v), 4) for v in statistic.max(axis=(0, 1))],
            "stat_mean": [round(float(v), 4) for v in statistic.mean(axis=(0, 1))],
            "stretch_low_value": attributes["stretch_low_value"],
            "stretch_high_value": attributes["stretch_high_value"],
            "stretch_domain": attributes["stretch_domain"],
            "p_low": options.p_low,
            "p_high": options.p_high,
            "destripe": options.destripe,
            "smooth": options.smooth,
            "image_width": int(image.shape[1]),
            "image_height": int(image.shape[0]),
        },
        images={"reduce": lambda: image},
    )


# ---------------- compare ----------------

class CompareOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold: int = Field(default=10, description="变化像素判定阈值（每像素三通道最大差，0-255）")
    colormap: str = Field(default="fire", description="差异图伪彩方案：gray / fire")


def _compare(call: Invocation) -> Outcome:
    options: CompareOptions = call.options  # type: ignore[assignment]
    rect = rect_of(call.address, *_size(call))
    result = core.compare_frames(
        call.media, **pair_options(call.address), rect=rect,
        threshold=options.threshold, colormap=options.colormap,
    )
    return Outcome(
        data={
            "frame_a": result.frame_a,
            "frame_b": result.frame_b,
            "time_a": result.time_a,
            "time_b": result.time_b,
            "rect": _rect_dict(rect),
            "threshold": result.threshold,
            "mean_abs_diff": result.mean_abs_diff,
            "max_abs_diff": result.max_abs_diff,
            "changed_pixels": result.changed_pixels,
            "changed_ratio": result.changed_ratio,
            "bbox": _rect_dict(result.bbox),
        },
        images={"diff": lambda: result.diff_image},
        refs={"diff_bbox": f"rect({','.join(map(str, result.bbox))})"} if result.bbox else {},
    )


# ---------------- sheet ----------------

class SheetOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(default=9, description="抽取帧数")
    cols: Optional[int] = Field(default=None, description="网格列数（缺省自动取近似方形）")
    tile_max_dim: int = Field(default=320, description="单格最大边长（像素）")
    annotate: bool = Field(default=True, description="是否绘制帧号/时间标注条")


def _sheet(call: Invocation) -> Outcome:
    options: SheetOptions = call.options  # type: ignore[assignment]
    when = range_options(call.address)
    when.pop("sample_every")
    result = core.sample_frames(
        call.media, count=options.count, cols=options.cols, **when,
        tile_max_dim=options.tile_max_dim, annotate=options.annotate,
        progress=call.progress,
    )
    return Outcome(
        data={
            "frames": result.frames,
            "times": result.times,
            "cols": result.cols,
            "rows": result.rows,
            "tile_width": result.tile_width,
            "tile_height": result.tile_height,
            "sheet_width": int(result.image.shape[1]),
            "sheet_height": int(result.image.shape[0]),
        },
        images={"sheet": lambda: result.image},
        refs={f"tile{n}": f"f={frame}" for n, frame in enumerate(result.frames, start=1)},
    )


# ---------------- scan ----------------

class ScanOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sheet_count: int = Field(default=9, description="概览网格抽帧数")
    threshold: Optional[float] = Field(default=None, description="事件分段阈值（缺省自动）")


def _scan(call: Invocation) -> Outcome:
    options: ScanOptions = call.options  # type: ignore[assignment]
    result = core.scan_media(
        call.media, sheet_count=options.sheet_count, sample_every=call.address.step,
        event_threshold=options.threshold, progress=call.progress, index=call.index,
    )
    images = {"sheet": lambda: result.sheet.image}
    if result.records:
        images["curve"] = lambda: render_curve(
            [r.normalized_score for r in result.records], y_min=0.0,
        )
    refs = _event_refs(result.events)
    refs.update({
        f"anomaly{n}": f"f={item['frame']}"
        for n, item in enumerate(result.anomalies, start=1)
    })
    return Outcome(
        data={
            "info": result.info.model_dump(),
            "effective_sample_every": result.effective_sample_every,
            "frames_analyzed": result.frames_analyzed,
            "sheet_frames": result.sheet.frames,
            "events": [e.to_dict() for e in result.events],
            "event_threshold_used": result.event_threshold,
            "anomalies": result.anomalies,
            "anomalies_truncated": result.anomalies_truncated,
            "computed_from": result.computed_from,
        },
        images=images,
        artifacts={"result": result},
        refs=refs,
    )


# ---------------- spectrum / spectrum2d ----------------

class SpectrumOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(default="luma", description="分析序列：luma（亮度）/ change（相邻帧变化量）")


def _spectrum_request(call: Invocation):
    options: SpectrumOptions = call.options  # type: ignore[assignment]
    width, height = _size(call)
    points = points_of(call.address, width, height)
    if len(points) > 1:
        raise AddressError("spectrum 一次只能分析一个点")
    return temporal_spectrum_request(
        call.media, source=options.source,
        rect=rect_of(call.address, width, height),
        point=points[0] if points else None,
        **range_options(call.address),
    )


def _spectrum(call: Invocation) -> Outcome:
    options: SpectrumOptions = call.options  # type: ignore[assignment]
    request = _spectrum_request(call)
    data_tensor, preview_tensor = pixelprobe.generate(request).request_tensors[0]
    _progress(call)
    attributes = data_tensor.attributes
    image = preview_tensor.data.materialize()
    return Outcome(
        data={
            "source": options.source,
            "samples": attributes["samples"],
            "effective_fps": attributes["effective_fps"],
            "nyquist_hz": attributes["nyquist_hz"],
            "dominant_freq_hz": attributes["dominant_freq_hz"],
            "period_seconds": attributes["period_seconds"],
            "period_frames": attributes["period_frames"],
            "peak_ratio": attributes["peak_ratio"],
            "top_peaks": attributes["top_peaks"],
            "vfr_warning": attributes["vfr_warning"],
        },
        images={"spectrum": lambda: image},
    )


def _spectrum2d_request(call: Invocation):
    return spatial_spectrum_request(
        call.media, **single_frame_options(call.address),
        rect=rect_of(call.address, *_size(call)),
    )


def _spectrum2d(call: Invocation) -> Outcome:
    request = _spectrum2d_request(call)
    data_tensor, preview_tensor = pixelprobe.generate(request).request_tensors[0]
    attributes = data_tensor.attributes
    image = preview_tensor.data.materialize()
    image_semantics = bool(request.feature.config["report_image_semantics"])
    return Outcome(
        data={
            "frame": None if image_semantics else attributes["frame"],
            "time_seconds": None if image_semantics else attributes["time_seconds"],
            "width": attributes["width"],
            "height": attributes["height"],
            "peaks": attributes["peaks"],
        },
        images={"spectrum": lambda: image},
    )


# ---------------- flow ----------------

class FlowOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accumulate: bool = Field(default=False, description="累积模式：对时间范围逐对光流相加")
    compensate: bool = Field(default=False, description="估计并扣除全局运动（区分镜头运动与物体运动）")
    mag_threshold: float = Field(default=1.0, description="运动区域判定的位移阈值（像素）")


def _flow_request(call: Invocation):
    options: FlowOptions = call.options  # type: ignore[assignment]
    if options.accumulate:
        if call.address.time is not None and call.address.time.mode == "list":
            raise AddressError('累积光流需要连续范围，例如 "f=0..30"')
        when = range_options(call.address)
    else:
        if call.address.step is not None:
            raise AddressError("两帧光流不接受 step=；累积模式请加 accumulate")
        when = pair_options(call.address)
    return flow_request(
        call.media, **when, accumulate=options.accumulate,
        compensate_global=options.compensate, mag_threshold=options.mag_threshold,
    )


def _flow(call: Invocation) -> Outcome:
    options: FlowOptions = call.options  # type: ignore[assignment]
    request = _flow_request(call)
    tensors = pixelprobe.generate(request).request_tensors[0]
    _progress(call)
    flow_preview, magnitude_preview = tensors[-2:]
    metadata = tensors[-3].attributes
    bbox = metadata["motion_bbox"]
    return Outcome(
        data={
            "frame_a": metadata["frame_a"],
            "frame_b": metadata["frame_b"],
            "accumulated": metadata["accumulated"],
            "frames_analyzed": metadata["frames_analyzed"],
            "mean_magnitude": metadata["mean_magnitude"],
            "max_magnitude": metadata["max_magnitude"],
            "p95_magnitude": metadata["p95_magnitude"],
            "dominant_angle_deg": metadata["dominant_angle_deg"],
            "global_motion": metadata["global_motion"],
            "compensated": metadata["compensated"],
            "motion_bbox": _rect_dict(bbox) if bbox else None,
            "mag_threshold": options.mag_threshold,
        },
        images={
            "flow": lambda: flow_preview.data.materialize(),
            "magnitude": lambda: magnitude_preview.data.materialize(),
        },
        refs={"motion_bbox": f"rect({','.join(map(str, bbox))})"} if bbox else {},
    )


TIMELINE = register(Capability(
    name="timeline", summary="固定像素（或网格采样点）的颜色时间线 [K,T,3]",
    run=_timeline, cli="timeline", mcp_tool="pixelprobe_measure", mcp_selector="timeline",
    address=AddressRule(_RANGE, frozenset({"point", "pid", "grid"}), step=True),
    options=TimelineOptions, image_names=("timeline",),
))
XT = register(Capability(
    name="xt", summary="X–T 时空切片：一整行像素随时间的变化（横轴 x，纵轴时间）",
    run=_slice("xt"), cli="xt", mcp_tool="pixelprobe_look", mcp_selector="xt",
    address=AddressRule(_RANGE, frozenset({"row"}), step=True), image_names=("slice",),
))
YT = register(Capability(
    name="yt", summary="Y–T 时空切片：一整列像素随时间的变化（横轴 y，纵轴时间）",
    run=_slice("yt"), cli="yt", mcp_tool="pixelprobe_look", mcp_selector="yt",
    address=AddressRule(_RANGE, frozenset({"col"}), step=True), image_names=("slice",),
))
CHANGES = register(Capability(
    name="changes", summary="相邻帧变化量与事件分段（点/区域/网格/整帧）",
    run=_changes, cli="changes", mcp_tool="pixelprobe_find", mcp_selector="changes",
    address=AddressRule(_RANGE, frozenset({"point", "pid", "rect", "cell", "grid"}), step=True),
    options=ChangesOptions, image_names=("curve",),
))
REDUCE = register(Capability(
    name="reduce", summary="时间域合成：把一段视频折叠为逐像素统计图（std/diff 显出隐藏图案与运动能量）",
    run=_reduce, cli="reduce", mcp_tool="pixelprobe_look", mcp_selector="reduce",
    address=AddressRule(_SPAN, frozenset({"rect", "cell"}), step=True),
    options=ReduceOptions, image_names=("reduce",),
))
COMPARE = register(Capability(
    name="compare", summary="比较两帧：差异热力图、变化像素数与变化区域",
    run=_compare, cli="compare", mcp_tool="pixelprobe_compare", mcp_selector="frames",
    address=AddressRule(_PAIR, frozenset({"rect", "cell"})),
    options=CompareOptions, image_names=("diff",),
))
SHEET = register(Capability(
    name="sheet", summary="在时间段内等距抽帧拼成标注了帧号和时间的网格图",
    run=_sheet, cli="sheet", mcp_tool="pixelprobe_look", mcp_selector="sheet",
    address=AddressRule(_SPAN), options=SheetOptions, image_names=("sheet",),
))
SCAN = register(Capability(
    name="scan", summary="一键概览：代表帧网格、变化事件、黑帧/白帧/纯色/闪帧候选",
    run=_scan, cli="scan", mcp_tool="pixelprobe_overview", mcp_selector="scan",
    address=AddressRule(step=True), options=ScanOptions, image_names=("sheet", "curve"),
))
SPECTRUM = register(Capability(
    name="spectrum", summary="时间域频谱：周期闪烁/周期变化的主频与周期",
    run=_spectrum, cli="spectrum", mcp_tool="pixelprobe_measure", mcp_selector="spectrum",
    address=AddressRule(_SPAN, frozenset({"point", "pid", "rect", "cell"}), step=True),
    options=SpectrumOptions, image_names=("spectrum",),
))
SPECTRUM2D = register(Capability(
    name="spectrum2d", summary="单帧二维频谱：条纹、摩尔纹与周期纹理",
    run=_spectrum2d, cli="spectrum2d", mcp_tool="pixelprobe_measure", mcp_selector="spectrum2d",
    address=AddressRule(_SINGLE, frozenset({"rect", "cell"})), image_names=("spectrum",),
))
FLOW = register(Capability(
    name="flow", summary="稠密光流：运动方向、幅度、全局运动与运动区域（需 OpenCV）",
    run=_flow, cli="flow", mcp_tool="pixelprobe_measure", mcp_selector="flow",
    address=AddressRule(frozenset({"list", "range", "all"}), step=True),
    options=FlowOptions, image_names=("flow", "magnitude"),
))


# 走引擎的能力：执行与资源预估共用同一个请求构造函数，保证预估的就是实际执行的请求
_ENGINE_REQUESTS = {
    "timeline": lambda call: _timeline_request(call)[0],
    "xt": lambda call: _slice_request(call, "xt")[0],
    "yt": lambda call: _slice_request(call, "yt")[0],
    "reduce": lambda call: _reduce_request(call)[0],
    "spectrum": _spectrum_request,
    "spectrum2d": _spectrum2d_request,
    "flow": _flow_request,
}


def engine_request(name: str, call: Invocation):
    """返回该能力将交给引擎执行的 RepresentationRequest；不走引擎的能力返回 None。"""
    builder = _ENGINE_REQUESTS.get(name)
    return builder(call) if builder is not None else None
