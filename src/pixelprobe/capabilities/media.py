"""单帧与媒体信息类能力：info、frame、pixel、region。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pixelprobe import core
from pixelprobe.address import points_of, rect_of, single_frame_options
from pixelprobe.capabilities.base import (
    AddressRule,
    Capability,
    Invocation,
    Outcome,
    register,
)
from pixelprobe.models.errors import AddressError
from pixelprobe.utils.timecode import seconds_to_ms

_SINGLE = frozenset({"single"})
_NO_POINT = '请在地址中至少指定一个 point(x,y) 或 pid(n)，例如 "f=12 point(520,340)"'
_NO_REGION = '请在地址中指定区域，例如 "f=15 rect(4,4,10,10)" 或 "cell(C4)"'


def _rect_dict(rect: tuple[int, int, int, int] | None) -> dict | None:
    if rect is None:
        return None
    return {"x": rect[0], "y": rect[1], "width": rect[2], "height": rect[3]}


def _info(call: Invocation) -> Outcome:
    """基本信息；图片附通道语义与调色板/透明度标志，视频附索引状态（CLI 与 MCP 相同）。"""
    info = core.get_media_info(call.media)
    data = info.model_dump()
    warnings: list[str] = []
    refs: dict[str, str] = {}
    if info.media_type == "image":
        from pixelprobe.capabilities.image_report import (
            image_analysis_flags, image_content_analysis_payload, native_image_metadata_payload,
        )
        from pixelprobe.core.image_reader import ImageReader
        with ImageReader() as reader:
            reader.open(call.media)
            analysis = reader.get_content_analysis()
            data["image_samples"] = {
                "native": native_image_metadata_payload(reader.get_native_metadata()),
                "engine_sample_semantics": reader.engine_sample_semantics(),
                "engine_conversion_flags": list(reader.engine_conversion_flags()),
            }
        data["image_analysis"] = image_content_analysis_payload(analysis)
        data["flags"] = image_analysis_flags(analysis)
        if analysis.pattern_assessment == "candidate":
            warnings.append("检测到规则高频纹理候选；它可能影响视觉识别，但不能单独证明压缩、损坏或伪造")
    elif call.index is None:
        data["index"] = {
            "status": "missing",
            "hint": "长视频可先建立索引（命令行 pixelprobe index build；MCP pixelprobe_index action=build），"
                    "之后整帧变化检测直接查表",
        }
    else:
        segments = call.index.segments
        data["index"] = {"status": "ready", **call.index.summary(), "segments": segments}
        refs = {
            f"seg{n}": f"f={item['start_frame']}..{item['end_frame']}"
            for n, item in enumerate(segments, start=1)
        }
    return Outcome(data=data, warnings=warnings, refs=refs)


def _frame(call: Invocation) -> Outcome:
    size = (0, 0)
    if any(type(shape).__name__ == "Cell" for shape in call.address.shapes):
        info = core.get_media_info(call.media)
        size = (info.width, info.height)
    crop = rect_of(call.address, *size)
    arr, idx, t, media_info = core.get_frame(
        call.media, **single_frame_options(call.address), crop=crop,
    )
    return Outcome(
        data={
            "path": media_info.path,
            "media_type": media_info.media_type,
            "frame": idx,
            "time_seconds": t,
            "time_ms": seconds_to_ms(t) if t is not None else None,
            "source_width": media_info.width,
            "source_height": media_info.height,
            "width": int(arr.shape[1]),
            "height": int(arr.shape[0]),
            "crop": _rect_dict(crop),
        },
        images={"frame": lambda: arr},
    )


class PixelOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample: Literal["display", "native"] = Field(
        default="display",
        description="display：历史兼容的显示 RGB8；native：图片原生通道样本",
    )


def _pixel(call: Invocation) -> Outcome:
    options: PixelOptions = call.options  # type: ignore[assignment]
    if options.sample == "native":
        if call.address.time is not None:
            raise AddressError("原生图片样本不接受时间（f=/t=）；时间只对视频有效")
        arr, metadata, media_info = core.load_native_image(call.media)
        points = points_of(call.address, media_info.width, media_info.height)
        if not points:
            raise AddressError(_NO_POINT)
        samples = core.inspect_native_pixels(
            arr, points, bands=metadata.bands, sample_semantics=metadata.sample_semantics,
        )
        return Outcome(data={
            "path": media_info.path,
            "media_type": media_info.media_type,
            "width": media_info.width,
            "height": media_info.height,
            "sample_semantics": metadata.sample_semantics,
            "native_image": {
                "mode": metadata.mode,
                "source_format": metadata.source_format,
                "dtype": metadata.dtype,
                "shape": list(metadata.shape),
                "bands": list(metadata.bands),
                "bits_per_sample": metadata.bits_per_sample,
                "has_alpha": metadata.has_alpha,
                "alpha_representation": metadata.alpha_representation,
            },
            "pixels": samples,
        })
    arr, idx, t, media_info = core.load_frame(call.media, **single_frame_options(call.address))
    points = points_of(call.address, media_info.width, media_info.height)
    if not points:
        raise AddressError(_NO_POINT)
    samples = core.inspect_pixels(arr, points, frame=idx, time_seconds=t)
    return Outcome(data={
        "path": media_info.path,
        "media_type": media_info.media_type,
        "width": media_info.width,
        "height": media_info.height,
        "frame": idx,
        "time_seconds": t,
        "time_ms": seconds_to_ms(t) if t is not None else None,
        "pixels": [sample.model_dump() for sample in samples],
    })


def _region(call: Invocation) -> Outcome:
    arr, idx, t, media_info = core.load_frame(call.media, **single_frame_options(call.address))
    rect = rect_of(call.address, media_info.width, media_info.height)
    if rect is None:
        raise AddressError(_NO_REGION)
    stats = core.analyze_region(arr, rect)
    x, y, w, h = rect
    return Outcome(
        data={
            "path": media_info.path,
            "media_type": media_info.media_type,
            "frame": idx,
            "time_seconds": t,
            "time_ms": seconds_to_ms(t) if t is not None else None,
            "statistics": stats.model_dump(),
        },
        images={"crop": lambda: arr[y: y + h, x: x + w, :]},
        artifacts={"rect": rect},
    )


INFO = register(Capability(
    name="info", summary="读取图片或视频的基本信息（尺寸、帧率、帧数、编码等）",
    run=_info, cli="info", mcp_tool="pixelprobe_overview", mcp_selector="meta",
))
FRAME = register(Capability(
    name="frame", summary="提取指定帧，可用 rect/cell 裁剪；不缩放",
    run=_frame, cli="frame", mcp_tool="pixelprobe_look", mcp_selector="frame",
    address=AddressRule(_SINGLE, frozenset({"rect", "cell"})),
    image_names=("frame",),
))
PIXEL = register(Capability(
    name="pixel", summary="读取一个或多个像素的 RGB/HEX/HSV/Lab/亮度，或图片原生通道值",
    run=_pixel, cli="pixel", mcp_tool="pixelprobe_measure", mcp_selector="pixel",
    address=AddressRule(_SINGLE, frozenset({"point", "pid"})),
    options=PixelOptions,
))
REGION = register(Capability(
    name="region", summary="统计区域的 RGB/HSV/Lab/亮度均值、中位数、最值与标准差",
    run=_region, cli="region", mcp_tool="pixelprobe_measure", mcp_selector="region",
    address=AddressRule(_SINGLE, frozenset({"rect", "cell"})),
    image_names=("crop",),
))
