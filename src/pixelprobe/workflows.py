"""有界流式组合分析，保留数值、覆盖范围和可校验结果包。"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pixelprobe.artifacts import Bundle, BundleWriter, sha256_file
from pixelprobe.artifacts.models import SourceRecord
from pixelprobe.core.color import luminance_array
from pixelprobe.core.frame_compare import absolute_difference
from pixelprobe.core.media_reader import detect_actual_format, get_media_info, load_frame
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.axes import AxisKind, AxisSpec, ChannelSpec
from pixelprobe.domain.coordinates import CoordinateSpace, CoordinateSpaceKind
from pixelprobe.domain.errors import MediaChangedDuringAnalysisError
from pixelprobe.domain.media import MediaIdentity
from pixelprobe.domain.references import ProvenanceRef
from pixelprobe.domain.tensor import MemoryArrayHandle, TensorField
from pixelprobe.engine.execution import CancellationToken
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.operators.base import ResourcePolicy
from pixelprobe.utils.resource_budget import ResourceBudget, budget_scope, check_active, require_memory


class WorkflowOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workflow: Literal["first_change", "compare_media", "quality_overview"]
    start_frame: int = Field(default=0, ge=0)
    end_frame: int = Field(default=1799, ge=0)
    sample_every: int = Field(default=1, ge=1)
    threshold: int = Field(default=10, ge=0, le=255)
    min_changed_ratio: float = Field(default=0.0, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounded_selection(self):
        if self.end_frame < self.start_frame:
            raise ValueError("结束帧不能早于起始帧")
        if (self.end_frame - self.start_frame) // self.sample_every + 1 > 10000:
            raise ValueError("组合分析最多保留 10000 个采样记录，请缩小范围或增大采样间隔")
        return self


@dataclass(frozen=True)
class WorkflowResult:
    report: dict
    bundle: Bundle


COLUMNS = ["frame", "time_seconds", "reference_frame", "reference_time_seconds",
           "mean_abs_difference", "max_abs_difference", "changed_ratio",
           "mean_luminance", "std_luminance", "min_rgb", "max_rgb"]
UNITS = ["presentation_index", "second", "presentation_index", "second",
         "code_value", "code_value", "ratio", "code_value", "code_value", "code_value", "code_value"]


def _identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


class _Frames:
    def __init__(self, path, info, options, stack):
        self.info, self.options, self.path = info, options, path
        self.reader = None
        self.returned = 0
        self.first = self.last = None
        self.flags = {}
        self.exhausted = False
        if info.media_type == "video":
            self.reader = stack.enter_context(VideoReader())
            self.reader.open(path)
            self.iterator = self.reader.iter_frame_packets(
                options.start_frame, options.end_frame, options.sample_every)
            stack.callback(self.iterator.close)
        else:
            if options.start_frame != 0:
                raise InvalidRangeError("图片仅包含展示帧 0")
            self.iterator = None

    def next(self):
        check_active()
        if self.exhausted:
            return None
        if self.iterator is None:
            if self.returned:
                self.exhausted = True
                return None
            item = (0, 0.0, load_frame(self.path)[0])
        else:
            packet = next(self.iterator, None)
            if packet is None:
                self.exhausted = True
                return None
            for flag in packet.flags:
                self.flags[flag] = self.flags.get(flag, 0) + 1
            item = (packet.presentation_index, packet.timeline_time_seconds, packet.data)
        self.returned += 1
        self.first = item[0] if self.first is None else self.first
        self.last = item[0]
        return item

    def coverage(self, analyzed):
        decoded = self.reader._last_decoded_frame_count if self.reader else self.returned
        converted = self.reader._last_converted_frame_count if self.reader else self.returned
        return {
            "requested_start_frame": self.options.start_frame,
            "requested_end_frame_inclusive": self.options.end_frame,
            "sample_every": self.options.sample_every,
            "selected_frames_returned": self.returned, "frames_analyzed": analyzed,
            "first_returned_frame": self.first, "last_returned_frame": self.last,
            "last_analyzed_frame": self.options.start_frame + (analyzed - 1) * self.options.sample_every if analyzed else None,
            "source_frames_decoded": decoded, "frames_converted": converted,
            "eof_confirmed": self.info.media_type == "image" or
                             (self.exhausted and decoded < self.options.end_frame + 1),
            "sampling_gaps": self.options.sample_every > 1,
            "timestamp_flags": self.flags,
            "source_frame_count_metadata": self.info.frame_count,
            "metadata_count_is_estimate": self.info.frame_count_estimated,
        }


def _tensor(name, values, *, attributes, image=False):
    accuracy = AccuracyInfo(level=AccuracyLevel.DERIVED, source="pixelprobe.workflow.v1",
                            unit="code_value" if image else "mixed")
    if image:
        height, width, _ = values.shape
        axes = (AxisSpec(name="y", kind=AxisKind.Y, length=height, unit="pixel"),
                AxisSpec(name="x", kind=AxisKind.X, length=width, unit="pixel"),
                AxisSpec(name="channel", kind=AxisKind.CHANNEL, length=3))
        channels = tuple(ChannelSpec(name=c, semantic=f"abs_difference_{c}",
                         unit="code_value", value_range=(0, 255), accuracy=accuracy) for c in ("r", "g", "b"))
        space = CoordinateSpace(coordinate_space_id="storage_pixels", kind=CoordinateSpaceKind.STORAGE,
                                axes=("x", "y"), width=width, height=height)
    else:
        axes = (AxisSpec(name="record", kind=AxisKind.BATCH, length=len(values)),
                AxisSpec(name="feature", kind=AxisKind.FEATURE, length=len(COLUMNS)))
        channels, space = (), None
    return TensorField(tensor_id=name, data=MemoryArrayHandle(np.ascontiguousarray(values)),
                       axes=axes, channels=channels, coordinate_space=space, axis_mappings=(),
                       validity=None if image else MemoryArrayHandle(np.isfinite(values)),
                       accuracy=accuracy, provenance=ProvenanceRef(provenance_id="prov_" + name),
                       attributes=attributes)


def run_workflow(path: Path, *, output_path: Path, options: WorkflowOptions,
                 reference_path: Path | None = None, cancellation: CancellationToken | None = None,
                 resources: ResourcePolicy | None = None) -> WorkflowResult:
    if (reference_path is not None) != (options.workflow == "compare_media"):
        raise InvalidRangeError("仅 compare_media 必须提供第二个媒体")
    token = cancellation or CancellationToken()
    resources = resources or ResourcePolicy(max_memory_bytes=256 * 1024**2,
                                            max_temporary_bytes=512 * 1024**2)
    with budget_scope(ResourceBudget(resources.max_memory_bytes, resources.max_temporary_bytes,
                                    token.raise_if_cancelled)):
        return _run(path, reference_path, output_path, options, token)


def _run(path, reference_path, output_path, options, token):
    paths = [Path(path).resolve(strict=True)]
    if reference_path is not None:
        paths.append(Path(reference_path).resolve(strict=True))
    identities = [_identity(p) for p in paths]
    hashes = [sha256_file(p) for p in paths]
    infos = [get_media_info(p) for p in paths]
    formats = [detect_actual_format(p) for p in paths]
    if options.workflow == "first_change" and infos[0].media_type != "video":
        raise InvalidRangeError("首次变化定位需要视频")
    if len(infos) == 2 and (infos[0].media_type, infos[0].width, infos[0].height) != (
            infos[1].media_type, infos[1].width, infos[1].height):
        raise InvalidRangeError("媒体比较要求相同媒体类型和尺寸，不自动缩放或对齐")
    records_limit = (options.end_frame - options.start_frame) // options.sample_every + 1
    require_memory(sum(i.width * i.height * 96 for i in infos) + records_limit * 2048)
    rows, first_change, best_diff, best_pair = [], None, None, None
    best_score = -1.0
    quality = {"black_candidates": 0, "white_candidates": 0, "flat_candidates": 0}
    stop_reason = "selection_exhausted"
    with ExitStack() as stack:
        frames = [_Frames(p, i, options, stack) for p, i in zip(paths, infos)]
        previous = None
        while True:
            token.raise_if_cancelled()
            current = frames[0].next()
            reference = frames[1].next() if len(frames) == 2 else previous
            if current is None or (len(frames) == 2 and reference is None):
                if len(frames) == 2 and (current is None) != (reference is None):
                    stop_reason = "unpaired_sample"
                break
            index, timestamp, array = current
            lum = luminance_array(array)
            mean_lum, std_lum = float(lum.mean()), float(lum.std())
            low, high = int(array.min()), int(array.max())
            quality["black_candidates"] += int(high < 16)
            quality["white_candidates"] += int(low > 240)
            quality["flat_candidates"] += int(std_lum < 2)
            row = [float(index), timestamp, np.nan, np.nan, np.nan, np.nan, np.nan,
                   mean_lum, std_lum, low, high]
            if reference is not None:
                ref_index, ref_time, ref_array = reference
                diff = absolute_difference(array, ref_array)
                per_pixel = diff.max(axis=2)
                score = float(diff.mean())
                ratio = float(np.count_nonzero(per_pixel > options.threshold) / per_pixel.size)
                row[2:7] = [ref_index, ref_time, score, int(per_pixel.max()), ratio]
                pair = {"frame": index, "time_seconds": timestamp, "reference_frame": ref_index,
                        "reference_time_seconds": ref_time, "mean_abs_difference": score,
                        "changed_ratio": ratio}
                ys, xs = np.nonzero(per_pixel > options.threshold)
                pair["changed_bbox"] = (
                    [int(xs.min()), int(ys.min()), int(xs.max() - xs.min()) + 1, int(ys.max() - ys.min()) + 1]
                    if len(xs) else None
                )
                if score > best_score:
                    best_score, best_diff, best_pair = score, diff.copy(), pair
                if first_change is None and ratio > options.min_changed_ratio:
                    first_change = pair
            rows.append(row)
            if options.workflow == "first_change" and first_change is not None:
                best_diff, best_pair = diff.copy(), first_change
                stop_reason = "first_threshold_crossing"
                break
            previous = current if len(frames) == 1 else None
        coverage = [f.coverage(len(rows)) for f in frames]
    if not rows:
        raise InvalidRangeError("指定范围没有可分析或可配对的帧")
    values = np.asarray(rows, dtype=np.float64)
    report = {
        "schema_version": "0.1.0", "workflow": options.workflow,
        "parameters": options.model_dump(), "source_sha256": hashes,
        "coverage": coverage, "stop_reason": stop_reason,
        "first_change": first_change, "difference_artifact_pair": best_pair,
        "difference_selection": "first_candidate" if options.workflow == "first_change" else "largest_mean_difference",
        "quality_source": "source_0",
        "quality_candidates": quality, "records": len(rows),
        "comparison_pairs": int(np.isfinite(values[:, 4]).sum()),
        "max_pair_time_delta_seconds": (
            float(np.max(np.abs(values[:, 1] - values[:, 3]))) if len(paths) == 2 else None
        ),
        "comparison_alignment": "presentation_index" if len(paths) == 2 else "consecutive_selected_frames",
        "automatic_authenticity_conclusion": None,
        "limitations": [
            "仅覆盖所列范围和采样帧；范围外、采样间隙及未配对尾部未检查",
            "首次变化指所检帧对中首个超阈值候选；sample_every > 1 时不能精确到间隙内首帧",
            "数值基于解码 RGB8，不比较音频、原生高位深、Alpha 或语义内容",
            "图片仅分析首张或首帧，不展开动图或多页图片",
            "按展示帧号比较不表示时间同步；时间戳异常保留标记，不用平均帧率重新对齐",
            "亮度与纯色候选可能是正常画面；不输出主观质量分或真伪判定",
            "输入完整性哈希需完整读取源文件两次，局部解码不消除该 I/O 成本",
        ],
    }
    attrs = {"artifact_role": "data", "dag_node_id": "workflow.metrics",
             "dag_input_node_ids": [], "operator": "pixelprobe.workflow.metrics",
             "workflow_report": report, "metric_names": COLUMNS, "metric_units": UNITS,
             "value_definition": "RGB8 absolute difference; changed=max_channel_diff>threshold; luminance=0.2126R+0.7152G+0.0722B without linearization",
             "quality_thresholds": {"black_max_rgb_exclusive": 16, "white_min_rgb_exclusive": 240,
                                    "flat_luminance_std_exclusive": 2},
             "parameters": options.model_dump(), "limitations": report["limitations"]}
    tensors = [_tensor("workflow_metrics", values, attributes=attrs)]
    if best_diff is not None:
        diff_attrs = {"artifact_role": "data", "dag_node_id": "workflow.difference",
                      "dag_input_node_ids": [], "operator": "pixelprobe.absolute_difference",
                      "pair": best_pair, "value_definition": "per_channel_absolute_difference",
                      "parameters": options.model_dump(), "limitations": report["limitations"]}
        tensors.append(_tensor("workflow_difference", best_diff, attributes=diff_attrs, image=True))
        preview_attrs = {**diff_attrs, "artifact_role": "preview", "dag_node_id": "workflow.preview",
                         "dag_input_node_ids": ["workflow.difference"], "source_tensor_id": "workflow_difference",
                         "operator": "pixelprobe.identity_preview"}
        tensors.append(_tensor("workflow_preview", best_diff, attributes=preview_attrs, image=True))
    sources = []
    for n, (p, identity, digest, actual_format) in enumerate(zip(paths, identities, hashes, formats)):
        token.raise_if_cancelled()
        if _identity(p) != identity or sha256_file(p) != digest:
            raise MediaChangedDuringAnalysisError("组合分析期间输入媒体发生变化")
        sources.append(SourceRecord(source_id=f"source_{n}", media_identity=MediaIdentity(
            source_id=f"source_{n}", size_bytes=identity[2], sha256=digest,
            file_id=f"{identity[0]:x}:{identity[1]:x}", modified_time_ns=identity[3],
            actual_format=actual_format), metadata_policy="safe"))
    bundle = BundleWriter().write(output_path, tuple(tensors), sources=tuple(sources))
    report["bundle_path"] = str(bundle.root)
    return WorkflowResult(report, bundle)
