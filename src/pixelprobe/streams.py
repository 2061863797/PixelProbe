"""视频容器与编码结构：取证与质量排查用的可复核线索。

只读取容器、流参数和压缩包（packet）序列，缺省不解码画面；`frame_types=True` 时再顺序解码一遍
统计 I/P/B 帧类型。输出的是结构事实与据此给出的"线索"，每条线索附证据与局限，
不判定是否经过重新编码、剪辑或伪造。

关键定义：
- GOP 长度：解码顺序中相邻两个关键帧包之间的包数（最后一段到流末尾，单独标注、不计入规律性判断）。
- 帧类型：I/P/B；S 为 GMC，小写 i/p/b 为 SI/SP/BI，? 为未标注。
- 时间戳问题：按解码顺序检查 dts 不递增；按展示时间排序后检查 pts 重复与超过中位间隔 1.5 倍的空隙。
- 每秒字节：按包的展示时间（pts × time_base）分桶累加包大小，反映码率随时间的变化。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from pixelprobe.core.video_reader import VideoReader

MAX_TAGS = 60
MAX_TAG_CHARS = 300
MAX_SERIES = 600
GAP_FACTOR = 1.5
LIMITATIONS = [
    "容器标签与编码参数可被任意改写，只能作为线索，不能证明来源或真伪",
    "封装器标签（如 Lavf）只说明最后一次封装所用的软件，不等于重新编码",
    "GOP 不规则可能来自场景切换自适应关键帧、直播推流或剪辑拼接，需结合画面判断",
    "未解码画面时帧类型未知；B 帧存在与否由 pts/dts 顺序推断",
]


# FFmpeg AVPictureType：PyAV 不同版本返回枚举名或整数
_PICTURE_TYPES = {0: "?", 1: "I", 2: "P", 3: "B", 4: "S", 5: "i", 6: "p", 7: "b"}


def _picture_type(value: Any) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return {"NONE": "?", "SI": "i", "SP": "p", "BI": "b"}.get(name, name[:1])
    try:
        return _PICTURE_TYPES.get(int(value), "?")
    except (TypeError, ValueError):
        return "?"


def _tags(metadata: Any) -> dict[str, str]:
    items = list(dict(metadata or {}).items())[:MAX_TAGS]
    return {str(key): str(value)[:MAX_TAG_CHARS] for key, value in items}


def _fraction(value: Any) -> str | None:
    return None if value is None else str(Fraction(value))


def _rate(value: Any) -> float | None:
    return None if not value else round(float(value), 6)


def _stats(values: list[float] | np.ndarray) -> dict[str, float] | None:
    array = np.asarray(values, dtype=np.float64)
    if array.size == 0:
        return None
    return {
        "count": int(array.size), "min": round(float(array.min()), 6),
        "max": round(float(array.max()), 6), "mean": round(float(array.mean()), 6),
        "median": round(float(np.median(array)), 6),
    }


def _stream_entry(stream: Any) -> dict[str, Any]:
    context = stream.codec_context
    entry: dict[str, Any] = {
        "index": stream.index, "type": stream.type,
        "codec": getattr(context, "name", None),
        "codec_long_name": getattr(getattr(context, "codec", None), "long_name", None),
        "profile": getattr(context, "profile", None),
        "bit_rate": stream.bit_rate or getattr(context, "bit_rate", None) or None,
        "time_base": _fraction(stream.time_base),
        "declared_frames": stream.frames or None,
        "duration_seconds": (
            round(float(stream.duration * stream.time_base), 6)
            if stream.duration is not None and stream.time_base else None
        ),
        "tags": _tags(stream.metadata),
    }
    if stream.type == "video":
        entry.update({
            "width": context.width, "height": context.height, "pix_fmt": context.pix_fmt,
            "color_range": getattr(context, "color_range", None),
            "colorspace": getattr(context, "colorspace", None),
            "color_primaries": getattr(context, "color_primaries", None),
            "color_trc": getattr(context, "color_trc", None),
            "sample_aspect_ratio": _fraction(getattr(context, "sample_aspect_ratio", None)),
            "average_rate": _rate(stream.average_rate),
            "base_rate": _rate(stream.base_rate),
            "guessed_rate": _rate(getattr(stream, "guessed_rate", None)),
            "has_b_frames_declared": bool(getattr(context, "has_b_frames", 0)),
        })
    elif stream.type == "audio":
        entry.update({
            "sample_rate": getattr(context, "sample_rate", None),
            "channels": getattr(context, "channels", None),
            "layout": getattr(getattr(context, "layout", None), "name", None),
        })
    return entry


def _packet_structure(packets: list[tuple[bool, int, int | None, int | None]], time_base: Fraction) -> dict:
    """packets：解码顺序的 (是否关键帧, 大小, pts, dts)。"""
    keyframes = [position for position, (key, *_rest) in enumerate(packets) if key]
    gops = [b - a for a, b in zip(keyframes, keyframes[1:])]
    tail = len(packets) - keyframes[-1] if keyframes else None
    sizes = np.asarray([size for _, size, _, _ in packets], dtype=np.float64)
    key_mask = np.asarray([key for key, *_ in packets], dtype=bool)
    dts = [value for *_, value in packets if value is not None]
    dts_regressions = sum(1 for a, b in zip(dts, dts[1:]) if b <= a)
    pts_decode_order = [pts for _, _, pts, _ in packets if pts is not None]
    reordered = any(b < a for a, b in zip(pts_decode_order, pts_decode_order[1:]))
    pts_sorted = sorted(pts_decode_order)
    duplicates = sum(1 for a, b in zip(pts_sorted, pts_sorted[1:]) if a == b)
    gaps: list[dict] = []
    steps = np.diff(np.asarray(pts_sorted, dtype=np.float64)) * float(time_base)
    typical = float(np.median(steps[steps > 0])) if np.any(steps > 0) else None
    if typical:
        for position in np.nonzero(steps > GAP_FACTOR * typical)[0][:50]:
            gaps.append({
                "after_seconds": round(float(pts_sorted[position] * time_base), 6),
                "interval_seconds": round(float(steps[position]), 6),
                "ratio": round(float(steps[position] / typical), 3),
            })
    per_second: dict[int, int] = {}
    for _, size, pts, _ in packets:
        if pts is not None:
            bucket = int(np.floor(float(pts * time_base)))
            per_second[bucket] = per_second.get(bucket, 0) + size
    seconds = sorted(per_second)
    series = [per_second[second] for second in seconds]
    regular = None
    if len(gops) >= 2:
        regular = len(set(gops)) == 1
    return {
        "packet_count": len(packets),
        "keyframe_count": len(keyframes),
        "keyframe_packet_positions": keyframes[:MAX_SERIES],
        "gop_lengths": gops[:MAX_SERIES],
        "gop_length_stats": _stats(gops),
        "gop_length_histogram": dict(Counter(gops).most_common(20)),
        "trailing_gop_length": tail,
        "regular_gop": regular,
        "b_frames_inferred": reordered,
        "dts_regressions": dts_regressions,
        "duplicate_pts": duplicates,
        "typical_frame_interval_seconds": None if typical is None else round(typical, 6),
        "timestamp_gaps": gaps,
        "packet_size_bytes": _stats(sizes),
        "keyframe_size_bytes": _stats(sizes[key_mask]),
        "interframe_size_bytes": _stats(sizes[~key_mask]),
        "bytes_per_second": {
            "start_second": seconds[0] if seconds else None,
            "values": series[:MAX_SERIES],
            "truncated": len(series) > MAX_SERIES,
        },
    }


def _clues(container_tags: dict, video: dict, packets: dict, frame_types: dict | None) -> list[dict]:
    clues: list[dict] = []
    encoder = container_tags.get("encoder") or container_tags.get("ENCODER")
    if encoder:
        clues.append({
            "code": "MUXER_TAG", "evidence": {"encoder": encoder},
            "message": "容器记录了封装软件；只说明最后一次封装所用工具，不等于重新编码",
        })
    creation = container_tags.get("creation_time") or video["tags"].get("creation_time")
    if not creation:
        clues.append({
            "code": "NO_CREATION_TIME", "evidence": {},
            "message": "容器与视频流都没有 creation_time 标签；常见于转码、录屏或剪辑软件导出，也可能被移除",
        })
    if packets["regular_gop"] is False:
        clues.append({
            "code": "IRREGULAR_GOP", "evidence": {"gop_length_histogram": packets["gop_length_histogram"]},
            "message": "关键帧间隔不固定；可能来自场景切换自适应关键帧、推流或拼接，需对照画面判断",
        })
    if packets["keyframe_count"] == 1 and packets["packet_count"] > 1:
        clues.append({
            "code": "SINGLE_KEYFRAME", "evidence": {"packet_count": packets["packet_count"]},
            "message": "整段只有一个关键帧；随机定位需要从头解码",
        })
    if packets["dts_regressions"] or packets["duplicate_pts"]:
        clues.append({
            "code": "TIMESTAMP_IRREGULARITY",
            "evidence": {"dts_regressions": packets["dts_regressions"], "duplicate_pts": packets["duplicate_pts"]},
            "message": "解码时间戳不递增或展示时间戳重复；可能来自拼接、封装错误或录制异常",
        })
    if packets["timestamp_gaps"]:
        clues.append({
            "code": "TIMESTAMP_GAPS", "evidence": {"count": len(packets["timestamp_gaps"])},
            "message": "展示时间存在超过中位帧间隔 1.5 倍的空隙；可能是掉帧、可变帧率或剪切",
        })
    average, base = video.get("average_rate"), video.get("base_rate")
    if average and base and abs(average - base) / base > 0.01:
        clues.append({
            "code": "VARIABLE_FRAME_RATE", "evidence": {"average_rate": average, "base_rate": base},
            "message": "平均帧率与标称帧率不一致，可能是可变帧率（录屏、手机拍摄常见）",
        })
    if frame_types is not None and not frame_types["counts"].get("B") and packets["b_frames_inferred"]:
        clues.append({
            "code": "REORDER_WITHOUT_B", "evidence": frame_types["counts"],
            "message": "包顺序存在重排但解码后未见 B 帧；请核对封装与时间戳",
        })
    return clues


def analyze_streams(
    path: Path, *, frame_types: bool = False,
    progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """返回容器、各流参数、主视频流的压缩包结构与可复核线索。"""
    with VideoReader() as reader:
        reader.open(Path(path))
        container, video = reader.container, reader.video_stream
        container_tags = _tags(container.metadata)
        report: dict[str, Any] = {
            "container": {
                "format": container.format.name,
                "format_long_name": container.format.long_name,
                "duration_seconds": None if container.duration is None else round(container.duration / 1_000_000, 6),
                "start_time_seconds": None if container.start_time is None else round(container.start_time / 1_000_000, 6),
                "bit_rate": container.bit_rate or None,
                "size_bytes": container.size or Path(path).stat().st_size,
                "tags": container_tags,
            },
            "streams": [_stream_entry(stream) for stream in container.streams],
            "video_stream_index": video.index,
        }
        expected = max(1, video.frames or 1)
        packets: list[tuple[bool, int, int | None, int | None]] = []
        for packet in container.demux(video):
            if cancel_check is not None and len(packets) % 256 == 0:
                cancel_check()
            if packet.size == 0 and packet.pts is None:
                continue  # 刷新用的空包
            packets.append((bool(packet.is_keyframe), int(packet.size), packet.pts, packet.dts))
            if progress is not None and len(packets) % 256 == 0:
                progress(len(packets), max(expected, len(packets)))
        structure = _packet_structure(packets, video.time_base or Fraction(1, 90000))
        types = None
        if frame_types:
            container.seek(0, stream=video)
            sequence: list[str] = []
            for frame in container.decode(video):
                if cancel_check is not None and len(sequence) % 64 == 0:
                    cancel_check()
                sequence.append(_picture_type(frame.pict_type))
            text = "".join(sequence)
            types = {"counts": dict(Counter(sequence)), "sequence": text[:2000],
                     "sequence_truncated": len(text) > 2000}
    video_entry = next(entry for entry in report["streams"] if entry["index"] == report["video_stream_index"])
    report["video_packets"] = structure
    report["frame_types"] = types
    report["clues"] = _clues(container_tags, video_entry, structure, types)
    report["limitations"] = LIMITATIONS
    report["automatic_authenticity_conclusion"] = None
    return report
