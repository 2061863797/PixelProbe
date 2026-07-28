"""将 1.0 前 CLI 参数精确转换为统一请求模型。"""

from __future__ import annotations

from pathlib import Path

from pixelprobe.core.frame_selector import resolve_range
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.domain.media import MediaSource
from pixelprobe.domain.time import TemporalSelection
from pixelprobe.domain.geometry import PathGeometry, PointGeometry
from pixelprobe.engine.request import (
    FeatureRequest,
    OutputRequest,
    ReductionRequest,
    RepresentationRequest,
)
from pixelprobe.core.timeline_extractor import build_points, sort_points
from pixelprobe.utils.coordinates import validate_point
from pixelprobe.utils.coordinates import validate_rect
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.core.media_reader import detect_media_type


def legacy_media_source(path: Path) -> MediaSource:
    resolved = Path(path).resolve(strict=True)
    return MediaSource(source_id="source_main", kind="file", uri=str(resolved))


def legacy_temporal_selection(
    path: Path,
    *,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
) -> TemporalSelection:
    """保留旧 CLI 的闭区间及“目标时间之前最后一帧”语义。"""
    with VideoReader() as reader:
        reader.open(Path(path))
        frame_range = resolve_range(
            reader,
            start_frame,
            end_frame,
            start,
            end,
            sample_every,
        )
    return TemporalSelection(
        mode="frame_interval",
        requested_start_frame=frame_range.start,
        requested_end_frame_exclusive=frame_range.end + 1,
        sample_every=frame_range.sample_every,
    )


def legacy_spacetime_request(
    path: Path,
    kind: str,
    fixed_coordinate: int,
    *,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
) -> RepresentationRequest:
    """构造 X-T/Y-T 请求，同时保留旧坐标错误与范围解析语义。"""
    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        if kind == "xt":
            validate_point(0, fixed_coordinate, info.width, info.height)
            points = (
                (0.0, float(fixed_coordinate)),
                (float(info.width - 1), float(fixed_coordinate)),
            )
        elif kind == "yt":
            validate_point(fixed_coordinate, 0, info.width, info.height)
            points = (
                (float(fixed_coordinate), 0.0),
                (float(fixed_coordinate), float(info.height - 1)),
            )
        else:
            raise ValueError(f"未知时空切片类型：{kind}")
        frame_range = resolve_range(
            reader, start_frame, end_frame, start, end, sample_every,
        )
    selection = TemporalSelection(
        mode="frame_interval",
        requested_start_frame=frame_range.start,
        requested_end_frame_exclusive=frame_range.end + 1,
        sample_every=frame_range.sample_every,
    )
    return RepresentationRequest(
        source=legacy_media_source(path),
        selection=selection,
        representation=kind,  # type: ignore[arg-type]
        geometry=PathGeometry(
            type="line", coordinate_space_id="storage_pixels", points=points,
        ),
        output=OutputRequest(format="memory", include_preview=False),
    )


def legacy_timeline_request(
    path: Path,
    *,
    points: list[tuple[int, int]] | None = None,
    pixel_ids: list[int] | None = None,
    grid: tuple[int, int, int, int] | None = None,
    step: int | None = None,
    block_size: int | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
    sort: str = "selection",
) -> tuple[RepresentationRequest, tuple[tuple[int, int], ...], int, int]:
    """构造离散点时间线请求，并返回旧输出需要的点序与尺寸。"""
    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        ordered = sort_points(
            build_points(
                info.width,
                info.height,
                points=points,
                pixel_ids=pixel_ids,
                grid=grid,
                step=step,
                block_size=block_size,
            ),
            sort,  # type: ignore[arg-type]
            info.width,
        )
        frame_range = resolve_range(
            reader, start_frame, end_frame, start, end, sample_every,
        )
    selection = TemporalSelection(
        mode="frame_interval",
        requested_start_frame=frame_range.start,
        requested_end_frame_exclusive=frame_range.end + 1,
        sample_every=frame_range.sample_every,
    )
    geometry = (
        PointGeometry(
            coordinate_space_id="storage_pixels",
            x=float(ordered[0][0]),
            y=float(ordered[0][1]),
        )
        if len(ordered) == 1
        else PathGeometry(
            type="polyline",
            coordinate_space_id="storage_pixels",
            points=tuple((float(x), float(y)) for x, y in ordered),
        )
    )
    return (
        RepresentationRequest(
            source=legacy_media_source(path),
            selection=selection,
            representation="points_t",
            geometry=geometry,
            feature=FeatureRequest(
                name="rgb",
                config={
                    "point_count": len(ordered),
                    "block_size": block_size,
                },
            ),
            output=OutputRequest(format="memory", include_preview=False),
        ),
        tuple(ordered),
        info.width,
        info.height,
    )


def legacy_reduce_request(
    path: Path,
    *,
    operation: str,
    rect: tuple[int, int, int, int] | None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
    p_low: float = 1.0,
    p_high: float = 99.0,
    destripe: bool = False,
    smooth: int = 0,
) -> RepresentationRequest:
    if operation not in {"mean", "median", "min", "max", "std", "diff"}:
        raise InvalidRangeError(
            f"op {operation!r} 无效，可选：mean/median/min/max/std/diff"
        )
    if not 0.0 <= p_low < p_high <= 100.0:
        raise InvalidRangeError(
            f"百分位范围无效：p_low={p_low}, p_high={p_high}"
            "（要求 0 <= p_low < p_high <= 100）"
        )
    if not 0 <= smooth <= 64:
        raise InvalidRangeError(f"smooth {smooth} 无效，必须在 0～64 内")
    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        if rect is not None:
            validate_rect(*rect, info.width, info.height)
        frame_range = resolve_range(
            reader, start_frame, end_frame, start, end, sample_every,
        )
    if operation in {"std", "diff"} and frame_range.count < 2:
        raise InvalidRangeError(
            f"op={operation} 至少需要两帧，请扩大帧范围或减小 sample_every"
        )
    return RepresentationRequest(
        source=legacy_media_source(path),
        selection=TemporalSelection(
            mode="frame_interval",
            requested_start_frame=frame_range.start,
            requested_end_frame_exclusive=frame_range.end + 1,
            sample_every=frame_range.sample_every,
        ),
        representation="frames",
        reduction=ReductionRequest(
            name=operation,
            axes=("time",),
            config={"rect": rect},
        ),
        output=OutputRequest(
            format="memory",
            include_preview=True,
            preview_config={
                "mode": "temporal_reduce",
                "p_low": p_low,
                "p_high": p_high,
                "destripe": destripe,
                "smooth": smooth,
            },
        ),
    )


def legacy_flow_request(
    path: Path,
    *,
    frame_a: int | None = None,
    time_a: float | None = None,
    frame_b: int | None = None,
    time_b: float | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
    accumulate: bool = False,
    compensate_global: bool = False,
    mag_threshold: float = 1.0,
) -> RepresentationRequest:
    if mag_threshold < 0:
        raise InvalidRangeError(f"mag_threshold {mag_threshold} 无效，必须 >= 0")
    with VideoReader() as reader:
        reader.open(Path(path))
        if accumulate:
            if any(value is not None for value in (frame_a, time_a, frame_b, time_b)):
                raise InvalidRangeError(
                    "累积模式使用帧范围参数，不能同时指定 frame_a/b 或 time_a/b"
                )
            frame_range = resolve_range(
                reader, start_frame, end_frame, start, end, sample_every,
            )
            if frame_range.count < 2:
                raise InvalidRangeError(
                    "累积光流至少需要两帧，请扩大帧范围或减小 sample_every"
                )
            selection = TemporalSelection(
                mode="frame_interval",
                requested_start_frame=frame_range.start,
                requested_end_frame_exclusive=frame_range.end + 1,
                sample_every=frame_range.sample_every,
            )
            pair = None
        else:
            if any(value is not None for value in (start_frame, end_frame, start, end)):
                raise InvalidRangeError(
                    "两帧模式使用 frame_a/b 或 time_a/b，不能同时指定帧范围参数"
                )
            if (frame_a is None) == (time_a is None):
                raise InvalidRangeError("帧 a 必须且只能指定 frame_a 或 time_a 之一")
            if (frame_b is None) == (time_b is None):
                raise InvalidRangeError("帧 b 必须且只能指定 frame_b 或 time_b 之一")
            index_a = frame_a if frame_a is not None else reader.frame_index_for_time(time_a)  # type: ignore[arg-type]
            index_b = frame_b if frame_b is not None else reader.frame_index_for_time(time_b)  # type: ignore[arg-type]
            # 精确校验显式帧；真正解码仍只在统一 Executor 中发生。
            if frame_a is not None:
                reader.validate_frame_index(frame_a)
            if frame_b is not None:
                reader.validate_frame_index(frame_b)
            pair = (int(index_a), int(index_b))
            selected = tuple(sorted(set(pair)))
            selection = TemporalSelection(mode="indices", requested_indices=selected)
    return RepresentationRequest(
        source=legacy_media_source(path),
        selection=selection,
        representation="feature_t",
        feature=FeatureRequest(
            name="farneback",
            config={
                "accumulate": accumulate,
                "compensate_global": compensate_global,
                "mag_threshold": mag_threshold,
                "frame_pair": pair,
            },
        ),
        output=OutputRequest(format="memory", include_preview=True),
    )


def legacy_temporal_spectrum_request(
    path: Path,
    *,
    source: str,
    rect: tuple[int, int, int, int] | None,
    point: tuple[int, int] | None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
) -> RepresentationRequest:
    if source not in {"change", "luma"}:
        raise InvalidRangeError(f"source {source!r} 无效，可选 change/luma")
    if rect is not None and point is not None:
        raise InvalidRangeError("rect 与 point 最多只能指定一个")
    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        if rect is not None:
            validate_rect(*rect, info.width, info.height)
        if point is not None:
            validate_point(*point, info.width, info.height)
        frame_range = resolve_range(
            reader, start_frame, end_frame, start, end, sample_every,
        )
    value_count = frame_range.count - (1 if source == "change" else 0)
    if value_count < 8:
        raise InvalidRangeError(
            f"频谱分析至少需要 8 个采样值（当前 {value_count}），"
            "请扩大帧范围或减小 sample_every"
        )
    return RepresentationRequest(
        source=legacy_media_source(path),
        selection=TemporalSelection(
            mode="frame_interval",
            requested_start_frame=frame_range.start,
            requested_end_frame_exclusive=frame_range.end + 1,
            sample_every=frame_range.sample_every,
        ),
        representation="feature_t",
        feature=FeatureRequest(
            name="temporal_fft",
            config={
                "source": source,
                "rect": rect,
                "point": point,
                "sample_every": sample_every,
                "vfr_policy": "estimate",
            },
        ),
        output=OutputRequest(format="memory", include_preview=True),
    )


def legacy_spatial_spectrum_request(
    path: Path,
    *,
    frame: int | None,
    time: float | None,
    rect: tuple[int, int, int, int] | None,
) -> RepresentationRequest:
    if frame is not None and time is not None:
        raise InvalidRangeError("--frame 与 --time 不能同时使用")
    is_image = detect_media_type(Path(path)) == "image"
    if is_image:
        if frame is not None or time is not None:
            raise InvalidRangeError("图片不支持 --frame / --time 参数")
        index = 0
    else:
        with VideoReader() as reader:
            reader.open(Path(path))
            index = (
                reader.frame_index_for_time(time)
                if time is not None else (frame if frame is not None else 0)
            )
            reader.validate_frame_index(index)
    return RepresentationRequest(
        source=legacy_media_source(path),
        selection=TemporalSelection(mode="indices", requested_indices=(index,)),
        representation="feature_t",
        feature=FeatureRequest(
            name="spatial_fft",
            config={"rect": rect, "report_image_semantics": is_image},
        ),
        output=OutputRequest(format="memory", include_preview=True),
    )
