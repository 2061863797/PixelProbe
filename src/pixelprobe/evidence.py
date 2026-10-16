"""图片证据分析：元数据、C2PA、ELA、局部噪声与边缘候选。"""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
from pathlib import Path
from typing import Any
import uuid

import numpy as np
from PIL import ExifTags, Image, ImageFilter

from pixelprobe.artifacts import Bundle, BundleWriter, sha256_file
from pixelprobe.artifacts.models import SourceRecord
from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.errors import MediaChangedDuringAnalysisError
from pixelprobe.domain.axes import AxisKind, AxisSpec, ChannelSpec
from pixelprobe.domain.coordinates import CoordinateSpace, CoordinateSpaceKind
from pixelprobe.domain.media import MediaIdentity
from pixelprobe.domain.references import ProvenanceRef
from pixelprobe.domain.tensor import MemoryArrayHandle, TensorField
from pixelprobe.engine.execution import CancellationToken
from pixelprobe.operators.base import ResourcePolicy
from pixelprobe.utils.resource_budget import (
    EVIDENCE_BYTES_PER_PIXEL, ResourceBudget, budget_scope, require_memory, check_active,
)


@dataclass(slots=True, frozen=True)
class EvidenceResult:
    report: dict[str, Any]
    bundle: Bundle


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, bytes):
        return {"hex_prefix": value[:128].hex(), "size_bytes": len(value)}
    if isinstance(value, dict):
        return {
            str(key)[:128]: _json_safe(item, depth=depth + 1)
            for key, item in list(value.items())[:512]
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value[:512]]
    return str(value)[:4096]


def _metadata(path: Path, image: Image.Image) -> dict[str, Any]:
    exif = {
        ExifTags.TAGS.get(int(tag), str(tag)): _json_safe(value)
        for tag, value in image.getexif().items()
    }
    return {
        "status": "completed",
        "format": image.format,
        "mode": image.mode,
        "width": image.width,
        "height": image.height,
        "exif": exif,
        "exif_entry_count": len(exif),
        "icc_profile_present": bool(image.info.get("icc_profile")),
        "xmp_present": bool(image.info.get("xmp")),
        "file_size_bytes": path.stat().st_size,
    }


def _c2pa(path: Path, *, trust_anchors_pem: str | None = None) -> dict[str, Any]:
    from pixelprobe.c2pa_adapter import verify_c2pa
    return _json_safe(verify_c2pa(path, trust_anchors_pem=trust_anchors_pem))


# 逐元素运算按行带分批，避免整图 float64/int16 临时数组；结果与整图计算逐位一致
_BAND_ROWS = 256


def _bands(height: int):
    for y0 in range(0, height, _BAND_ROWS):
        yield y0, min(y0 + _BAND_ROWS, height)


def _colorize(values: np.ndarray) -> np.ndarray:
    finite = np.nan_to_num(values.astype(np.float32, copy=False), nan=0.0, posinf=0.0, neginf=0.0)
    high = float(np.percentile(finite, 99.0)) if finite.size else 0.0
    out = np.empty((*finite.shape, 3), dtype=np.uint8)
    for y0, y1 in _bands(finite.shape[0]):
        scaled = np.zeros(finite[y0:y1].shape, dtype=np.uint8)
        if high > 0:
            scaled = np.clip(finite[y0:y1] / high * 255.0, 0, 255).astype(np.uint8)
        green = np.clip(255 - np.abs(scaled.astype(np.int16) - 128) * 2, 0, 255)
        out[y0:y1, :, 0] = scaled
        out[y0:y1, :, 1] = green.astype(np.uint8)
        out[y0:y1, :, 2] = 255 - scaled
    return out


def _block_candidates(
    values: np.ndarray, *, block_size: int, percentile: float = 95.0,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    height, width = values.shape
    heat = np.zeros((height, width), dtype=np.float32)
    blocks: list[dict[str, Any]] = []
    scores: list[float] = []
    for y in range(0, height, block_size):
        for x in range(0, width, block_size):
            region = values[y:min(y + block_size, height), x:min(x + block_size, width)]
            score = float(np.sqrt(np.mean(np.square(region, dtype=np.float64))))
            scores.append(score)
            blocks.append({
                "x": x, "y": y, "width": int(region.shape[1]),
                "height": int(region.shape[0]), "score": round(score, 6),
            })
            heat[y:y + region.shape[0], x:x + region.shape[1]] = score
    threshold = float(np.percentile(scores, percentile)) if scores else 0.0
    candidates = sorted(
        (item for item in blocks if item["score"] >= threshold and item["score"] > 0),
        key=lambda item: item["score"], reverse=True,
    )[:10]
    return heat, candidates


def _ela(rgb: np.ndarray, *, quality: int) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="JPEG", quality=quality, optimize=False)
    buffer.seek(0)
    with Image.open(buffer) as decoded:
        recompressed = np.asarray(decoded.convert("RGB"), dtype=np.uint8)
    raw_diff = np.empty_like(rgb)
    maximum = 0
    total = 0
    for y0, y1 in _bands(rgb.shape[0]):
        band = np.abs(rgb[y0:y1].astype(np.int16) - recompressed[y0:y1].astype(np.int16))
        raw_diff[y0:y1] = band
        maximum = max(maximum, int(band.max(initial=0)))
        total += int(band.sum(dtype=np.int64))  # 整数和精确，与整图 mean 的 float64 求和一致
    del recompressed
    preview = np.zeros_like(rgb, dtype=np.uint8)
    if maximum:
        for y0, y1 in _bands(rgb.shape[0]):
            preview[y0:y1] = np.clip(
                raw_diff[y0:y1].astype(np.int16) * (255.0 / maximum), 0, 255,
            ).astype(np.uint8)
    return raw_diff, preview, {
        "status": "completed", "jpeg_quality": quality,
        "max_absolute_difference": maximum,
        "mean_absolute_difference": round(float(total / raw_diff.size), 6),
        "recompressed_size_bytes": len(buffer.getbuffer()),
        "limitations": [
            "ELA 对保存质量、原始压缩历史和高纹理区域敏感",
            "亮区只是重压缩差异候选，不能单独证明编辑或拼接",
        ],
    }


def _noise(rgb: np.ndarray, *, block_size: int) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    blurred = np.asarray(Image.fromarray(rgb).filter(ImageFilter.BoxBlur(1)), dtype=np.uint8)
    residual = np.empty(rgb.shape[:2], dtype=np.float32)
    for y0, y1 in _bands(rgb.shape[0]):
        residual[y0:y1] = np.mean(
            np.abs(rgb[y0:y1].astype(np.float32) - blurred[y0:y1].astype(np.float32)), axis=2,
        )
    del blurred
    heat, candidates = _block_candidates(residual, block_size=block_size)
    mean_residual = round(float(residual.mean()), 6)
    std_residual = round(float(residual.std()), 6)
    del residual
    return heat, _colorize(heat), {
        "status": "completed", "block_size": block_size,
        "mean_residual": mean_residual,
        "std_residual": std_residual,
        "candidate_regions": candidates,
        "limitations": [
            "纹理、锐化、降噪、景深和局部压缩都会改变噪声残差",
            "候选区域需要结合原图、元数据和其他证据复核",
        ],
    }


def _edges(rgb: np.ndarray, *, block_size: int) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    magnitude = np.empty(rgb.shape[:2], dtype=np.float32)
    for y0, y1 in _bands(rgb.shape[0]):
        top = max(y0 - 1, 0)  # 多取上方一行，保证带间纵向差分与整图一致
        part = rgb[top:y1]
        gray = (
            0.299 * part[..., 0].astype(np.float32)
            + 0.587 * part[..., 1].astype(np.float32)
            + 0.114 * part[..., 2].astype(np.float32)
        )
        gx = np.zeros_like(gray)
        gy = np.zeros_like(gray)
        gx[:, 1:] = np.abs(np.diff(gray, axis=1))
        gy[1:, :] = np.abs(np.diff(gray, axis=0))
        magnitude[y0:y1] = np.hypot(gx, gy)[y0 - top:]
    heat, candidates = _block_candidates(magnitude, block_size=block_size)
    mean_gradient = round(float(magnitude.mean()), 6)
    del magnitude
    return heat, _colorize(heat), {
        "status": "completed", "block_size": block_size,
        "mean_gradient": mean_gradient,
        "candidate_regions": candidates,
        "limitations": [
            "自然物体轮廓、文字和高对比纹理会产生强边缘",
            "该热力图定位边缘异常候选，不直接识别拼接",
        ],
    }


def _ela_data_tensor(
    tensor_id: str,
    difference: np.ndarray,
    *,
    attributes: dict[str, Any],
) -> TensorField:
    height, width = difference.shape[:2]
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DERIVED,
        source="pixelprobe_evidence_ela",
        unit="code_value",
    )
    return TensorField(
        tensor_id=tensor_id,
        data=MemoryArrayHandle(np.ascontiguousarray(difference, dtype=np.uint8)),
        axes=(
            AxisSpec(name="y", kind=AxisKind.Y, length=height, unit="pixel"),
            AxisSpec(name="x", kind=AxisKind.X, length=width, unit="pixel"),
            AxisSpec(name="channel", kind=AxisKind.CHANNEL, length=3),
        ),
        channels=tuple(
            ChannelSpec(
                name=name,
                semantic=f"ela_abs_difference_{name}",
                unit="code_value",
                value_range=(0, 255),
                accuracy=accuracy,
            )
            for name in ("r", "g", "b")
        ),
        coordinate_space=CoordinateSpace(
            coordinate_space_id="storage_pixels",
            kind=CoordinateSpaceKind.STORAGE,
            axes=("x", "y"),
            width=width,
            height=height,
        ),
        axis_mappings=(),
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id="prov_evidence_ela"),
        attributes={
            "artifact_role": "data",
            "evidence_method": "ela",
            "value_definition": "per_channel_absolute_difference",
            "unit": "code_value",
            "dag_node_id": "evidence.data.ela",
            "dag_input_node_ids": [],
            "operator": "pixelprobe.evidence.ela",
            "parameters": {"jpeg_quality": attributes.get("jpeg_quality")},
            "limitations": list(attributes.get("limitations") or []),
            "parameters_and_limits": attributes,
        },
    )


def _heatmap_data_tensor(
    tensor_id: str,
    heat: np.ndarray,
    *,
    method: str,
    unit: str,
    value_definition: str,
    block_size: int,
    attributes: dict[str, Any],
) -> TensorField:
    height, width = heat.shape[:2]
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DERIVED,
        source=f"pixelprobe_evidence_{method}",
        unit=unit,
    )
    return TensorField(
        tensor_id=tensor_id,
        data=MemoryArrayHandle(np.ascontiguousarray(heat, dtype=np.float32)),
        axes=(
            AxisSpec(name="y", kind=AxisKind.Y, length=height, unit="pixel"),
            AxisSpec(name="x", kind=AxisKind.X, length=width, unit="pixel"),
        ),
        channels=(),
        coordinate_space=CoordinateSpace(
            coordinate_space_id="storage_pixels",
            kind=CoordinateSpaceKind.STORAGE,
            axes=("x", "y"),
            width=width,
            height=height,
        ),
        axis_mappings=(),
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id=f"prov_evidence_{method}"),
        attributes={
            "artifact_role": "data",
            "evidence_method": method,
            "value_definition": value_definition,
            "unit": unit,
            "block_size": block_size,
            "dag_node_id": f"evidence.data.{method}",
            "dag_input_node_ids": [],
            "operator": f"pixelprobe.evidence.{method}",
            "parameters": {"block_size": block_size},
            "limitations": list(attributes.get("limitations") or []),
            "parameters_and_limits": attributes,
        },
    )


def _preview_tensor(
    tensor_id: str,
    array: np.ndarray,
    *,
    method: str,
    source_tensor_id: str,
    source_dag_node_id: str,
    attributes: dict[str, Any],
) -> TensorField:
    height, width = array.shape[:2]
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DERIVED,
        source=f"pixelprobe_evidence_{method}",
        unit="display_value",
    )
    return TensorField(
        tensor_id=tensor_id,
        data=MemoryArrayHandle(np.ascontiguousarray(array, dtype=np.uint8)),
        axes=(
            AxisSpec(name="y", kind=AxisKind.Y, length=height, unit="pixel"),
            AxisSpec(name="x", kind=AxisKind.X, length=width, unit="pixel"),
            AxisSpec(name="channel", kind=AxisKind.CHANNEL, length=3),
        ),
        channels=tuple(
            ChannelSpec(
                name=name,
                semantic=f"evidence_preview_{name}",
                unit="display_value",
                value_range=(0, 255),
                accuracy=accuracy,
            )
            for name in ("r", "g", "b")
        ),
        coordinate_space=CoordinateSpace(
            coordinate_space_id="storage_pixels",
            kind=CoordinateSpaceKind.STORAGE,
            axes=("x", "y"),
            width=width,
            height=height,
        ),
        axis_mappings=(),
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id=f"prov_evidence_preview_{method}"),
        attributes={
            "artifact_role": "preview",
            "evidence_method": method,
            "source_tensor_id": source_tensor_id,
            "dag_node_id": f"evidence.preview.{method}",
            "dag_input_node_ids": [source_dag_node_id],
            "operator": f"pixelprobe.evidence.preview.{method}",
            "parameters_and_limits": attributes,
        },
    )


def _report_tensor(
    report: dict[str, Any],
    *,
    input_dag_node_ids: tuple[str, ...] = (),
) -> TensorField:
    names = ("ela_mean_difference", "noise_mean_residual", "edge_mean_gradient")
    ela_section = report.get("ela") or {}
    noise_section = report.get("noise_consistency") or {}
    edge_section = report.get("edge_candidates") or {}

    ela_valid = (
        ela_section.get("status") == "completed"
        and "mean_absolute_difference" in ela_section
    )
    noise_valid = (
        noise_section.get("status") == "completed"
        and "mean_residual" in noise_section
    )
    edge_valid = (
        edge_section.get("status") == "completed"
        and "mean_gradient" in edge_section
    )

    ela_val = (
        float(ela_section["mean_absolute_difference"])
        if ela_valid
        else float("nan")
    )
    noise_val = (
        float(noise_section["mean_residual"])
        if noise_valid
        else float("nan")
    )
    edge_val = (
        float(edge_section["mean_gradient"])
        if edge_valid
        else float("nan")
    )

    values = np.asarray([ela_val, noise_val, edge_val], dtype=np.float64)
    validity = np.asarray([ela_valid, noise_valid, edge_valid], dtype=bool)

    accuracy = AccuracyInfo(
        level=AccuracyLevel.DERIVED,
        source="pixelprobe_evidence_metrics",
        unit="mixed",
    )
    return TensorField(
        tensor_id="tensor_evidence_report",
        data=MemoryArrayHandle(values),
        axes=(AxisSpec(name="feature", kind=AxisKind.FEATURE, length=len(names)),),
        channels=(),
        coordinate_space=None,
        axis_mappings=(),
        validity=MemoryArrayHandle(validity),
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id="prov_evidence_report"),
        attributes={
            "artifact_role": "data",
            "metric_names": list(names),
            "dag_node_id": "evidence.report",
            "dag_input_node_ids": list(input_dag_node_ids),
            "operator": "pixelprobe.evidence.report",
            "evidence_report": report,
            "conclusion_policy": "只提供证据和候选，不自动判定媒体真伪",
        },
    )


def analyze_evidence(
    path: Path, *, output_path: Path, jpeg_quality: int = 90,
    block_size: int = 32, max_pixels: int = 64_000_000,
    cancellation: CancellationToken | None = None,
    resources: ResourcePolicy | None = None,
    c2pa_trust_anchors_pem: str | None = None,
) -> EvidenceResult:
    resources = resources or ResourcePolicy(
        max_memory_bytes=256 * 1024 * 1024,
        max_temporary_bytes=512 * 1024 * 1024,
    )
    token = cancellation or CancellationToken()
    with budget_scope(ResourceBudget(resources.max_memory_bytes,
                                    resources.max_temporary_bytes, token.raise_if_cancelled)):
        return _analyze_evidence(path, output_path=output_path,
                                 jpeg_quality=jpeg_quality, block_size=block_size,
                                 max_pixels=max_pixels, cancellation=token,
                                 c2pa_trust_anchors_pem=c2pa_trust_anchors_pem)


def _analyze_evidence(
    path: Path,
    *,
    output_path: Path,
    jpeg_quality: int = 90,
    block_size: int = 32,
    max_pixels: int = 64_000_000,
    cancellation: CancellationToken | None = None,
    c2pa_trust_anchors_pem: str | None = None,
) -> EvidenceResult:
    """独立执行各证据方法；单项失败会记录，其他方法继续。"""
    path = Path(path).resolve(strict=True)
    cancellation = cancellation if cancellation is not None else CancellationToken()
    cancellation.raise_if_cancelled()
    if not 1 <= jpeg_quality <= 100:
        raise ValueError("jpeg_quality 必须在 1～100 之间")
    if block_size < 8 or block_size > 512:
        raise ValueError("block_size 必须在 8～512 之间")
    report: dict[str, Any] = {
        "schema_version": "0.1.0",
        "source_sha256": sha256_file(path),
        "automatic_authenticity_conclusion": None,
    }
    data_tensors: list[TensorField] = []
    previews: list[TensorField] = []
    events: list[dict[str, object]] = []
    source_format = None
    # 限制必须在 Pillow 完整解码前检查，不能被逐项失败处理吞掉。
    with Image.open(path) as header:
        require_memory(header.width * header.height * EVIDENCE_BYTES_PER_PIXEL)
    try:
        with Image.open(path) as source:
            source_format = (source.format or "").upper() or None
            if source.width * source.height > max_pixels:
                raise ValueError(
                    f"图片包含 {source.width * source.height} 像素，超过证据分析限制 {max_pixels}"
                )
            source.load()
            report["metadata"] = _metadata(path, source)
            rgb = np.asarray(source.convert("RGB"), dtype=np.uint8)
    except Exception as exc:
        report.setdefault("metadata", {
            "status": "failed", "error_type": type(exc).__name__,
        })
        report["ela"] = report["noise_consistency"] = report["edge_candidates"] = {
            "status": "failed", "error_type": type(exc).__name__,
        }
        events.append({"type": "warning", "code": "IMAGE_ANALYSIS_FAILED"})
    else:
        check_active()
        cancellation.raise_if_cancelled()
        if source_format in {"JPEG", "JPG", "MPO"}:
            try:
                diff_data, preview, section = _ela(rgb, quality=jpeg_quality)
                report["ela"] = section
                data_tensors.append(_ela_data_tensor(
                    "tensor_evidence_ela", diff_data, attributes=section,
                ))
                previews.append(_preview_tensor(
                    "preview_evidence_ela",
                    preview,
                    method="ela",
                    source_tensor_id="tensor_evidence_ela",
                    source_dag_node_id="evidence.data.ela",
                    attributes=section,
                ))
            except Exception as exc:
                report["ela"] = {"status": "failed", "error_type": type(exc).__name__}
                events.append({"type": "warning", "code": "ELA_FAILED"})
        else:
            report["ela"] = {
                "status": "not_applicable", "reason": "ELA 仅适用于 JPEG 来源",
            }

        cancellation.raise_if_cancelled()
        try:
            heat, preview, section = _noise(rgb, block_size=block_size)
            report["noise_consistency"] = section
            data_tensors.append(_heatmap_data_tensor(
                "tensor_evidence_noise",
                heat,
                method="noise",
                unit="residual_rms",
                value_definition="block_score_heatmap_boxblur_residual_rms",
                block_size=block_size,
                attributes=section,
            ))
            previews.append(_preview_tensor(
                "preview_evidence_noise",
                preview,
                method="noise",
                source_tensor_id="tensor_evidence_noise",
                source_dag_node_id="evidence.data.noise",
                attributes=section,
            ))
        except Exception as exc:
            report["noise_consistency"] = {"status": "failed", "error_type": type(exc).__name__}
            events.append({"type": "warning", "code": "NOISE_FAILED"})

        cancellation.raise_if_cancelled()
        try:
            heat, preview, section = _edges(rgb, block_size=block_size)
            report["edge_candidates"] = section
            data_tensors.append(_heatmap_data_tensor(
                "tensor_evidence_edge",
                heat,
                method="edge",
                unit="gradient_rms",
                value_definition="block_score_heatmap_gradient_magnitude_rms",
                block_size=block_size,
                attributes=section,
            ))
            previews.append(_preview_tensor(
                "preview_evidence_edge",
                preview,
                method="edge",
                source_tensor_id="tensor_evidence_edge",
                source_dag_node_id="evidence.data.edge",
                attributes=section,
            ))
        except Exception as exc:
            report["edge_candidates"] = {"status": "failed", "error_type": type(exc).__name__}
            events.append({"type": "warning", "code": "EDGE_FAILED"})

    cancellation.raise_if_cancelled()
    report["c2pa"] = (
        _c2pa(path) if c2pa_trust_anchors_pem is None
        else _c2pa(path, trust_anchors_pem=c2pa_trust_anchors_pem)
    )
    if report["c2pa"]["verification_state"] in {"verification_failed", "not_checked"}:
        events.append({"type": "warning", "code": "C2PA_" + report["c2pa"]["verification_state"].upper()})
    check_active()
    cancellation.raise_if_cancelled()
    if sha256_file(path) != report["source_sha256"]:
        raise MediaChangedDuringAnalysisError("证据分析期间输入媒体发生变化")
    stat = path.stat()
    source_record = SourceRecord(
        source_id="source_main",
        media_identity=MediaIdentity(
            source_id="source_main", size_bytes=stat.st_size,
            sha256=report["source_sha256"],
            file_id=f"{stat.st_dev:x}:{stat.st_ino:x}",
            modified_time_ns=stat.st_mtime_ns,
            actual_format=source_format,
        ),
        metadata_policy="safe",
    )
    report_tensor = _report_tensor(
        report,
        input_dag_node_ids=tuple(t.attributes["dag_node_id"] for t in data_tensors),
    )
    bundle = BundleWriter().write(
        output_path,
        (report_tensor, *data_tensors, *previews),
        sources=(source_record,),
        events=tuple(events),
        execution_id="evidence-" + uuid.uuid4().hex,
    )
    report = {
        **report,
        "bundle_path": str(bundle.root),
        "bundle_id": bundle.manifest.bundle_id,
        "artifact_count": len(bundle.manifest.artifacts),
    }
    return EvidenceResult(report=report, bundle=bundle)
