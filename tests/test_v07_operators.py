"""V0.7 Operator、统一数值 Tensor、Path/ROI 与 NPY 验收测试。

所有表示都经统一引擎生成（`pixelprobe.generate`），不另设直接解码的实现。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from conftest import (
    FRAME_COUNT,
    GREEN_POS,
    RED_Y,
    engine_reduce,
    engine_tensors,
    engine_xt,
    expected_xt,
    make_frame,
)
from pixelprobe.artifacts import NpyArrayHandle, save_npy
from pixelprobe.domain.geometry import PathGeometry, RectGeometry
from pixelprobe.domain.time import TemporalSelection
from pixelprobe.engine.request import (
    FeatureRequest,
    OutputRequest,
    ReductionRequest,
    RepresentationRequest,
)
from pixelprobe.operators.base import OperatorSpec
from pixelprobe.operators.sampling import SamplingConfig, resample_polyline
from pixelprobe.request_builders import (
    flow_request,
    media_source,
    spatial_spectrum_request,
    temporal_spectrum_request,
    timeline_matrix,
    timeline_request,
)


def _memory() -> OutputRequest:
    return OutputRequest(format="memory", include_preview=False)


def test_operator_spec_rejects_unsafe_nondeterministic_cache() -> None:
    with pytest.raises(ValidationError):
        OperatorSpec(
            name="bad.random",
            version="1.0.0",
            category="transform",
            deterministic="nondeterministic",
            stateful=False,
            chunkable=False,
            cacheable=True,
            supported_dtypes=("float32",),
            config_schema_id="bad.v1",
        )


def test_sampling_config_has_schema_and_rejects_invalid_combinations() -> None:
    schema = SamplingConfig.model_json_schema()
    assert schema["properties"]["kind"]["title"] == "Kind"
    with pytest.raises(ValidationError):
        SamplingConfig(kind="roi_t")
    with pytest.raises(ValidationError):
        SamplingConfig(kind="path_t", block_size=2)
    with pytest.raises(ValidationError):
        SamplingConfig(kind="roi_t", reduction="mean", percentile=90)


def test_xt_is_normative_tensor_matching_ground_truth(test_video: Path) -> None:
    tensor = engine_xt(test_video, RED_Y)
    assert [axis.name for axis in tensor.axes] == ["time", "x", "channel"]
    assert tensor.data.dtype == "uint8"
    assert np.array_equal(tensor.data.materialize(), expected_xt(RED_Y))
    assert tensor.attributes["representation"] == "xt"


def test_timeline_matrix_only_transposes_tensor(test_video: Path) -> None:
    request, _, _, _ = timeline_request(test_video, points=[(0, RED_Y), (1, RED_Y)])
    tensor = engine_tensors(request)[0]
    data = tensor.data.materialize()
    assert data.shape == (FRAME_COUNT, 2, 3)
    assert [axis.name for axis in tensor.axes] == ["time", "path", "channel"]
    assert np.array_equal(timeline_matrix(tensor), np.transpose(data, (1, 0, 2)))


def test_path_t_matches_xt_on_integer_horizontal_path(test_video: Path) -> None:
    request = RepresentationRequest(
        source=media_source(test_video),
        selection=TemporalSelection(mode="all"),
        representation="path_t",
        geometry=PathGeometry(
            type="line", coordinate_space_id="storage_pixels",
            points=((0.0, float(RED_Y)), (31.0, float(RED_Y))),
        ),
        feature=FeatureRequest(
            name="rgb", config={"sample_count": 32, "interpolation": "nearest"},
        ),
        output=_memory(),
    )
    tensor = engine_tensors(request)[0]
    assert tensor.data.shape == (FRAME_COUNT, 32, 3)
    assert np.array_equal(tensor.data.materialize(), expected_xt(RED_Y))
    path_mapping = next(
        mapping for mapping in tensor.axis_mappings
        if mapping.output_axes == ("path",)
    )
    assert path_mapping.kind == "lookup"


def test_polyline_resampling_is_arc_length_regular() -> None:
    points = resample_polyline([(0.0, 0.0), (3.0, 0.0), (3.0, 4.0)], 8)
    distances = np.hypot(
        np.diff([point[0] for point in points]),
        np.diff([point[1] for point in points]),
    )
    assert points[0] == (0.0, 0.0)
    assert points[-1] == (3.0, 4.0)
    assert distances == pytest.approx(np.ones(7), abs=1e-12)


def test_roi_t_returns_full_precision_time_channel_tensor(test_video: Path) -> None:
    gx, gy = GREEN_POS
    request = RepresentationRequest(
        source=media_source(test_video),
        selection=TemporalSelection(mode="all"),
        representation="roi_t",
        geometry=RectGeometry(
            coordinate_space_id="storage_pixels", x=gx, y=gy, width=1, height=1,
        ),
        reduction=ReductionRequest(name="mean", axes=("time",)),
        output=_memory(),
    )
    tensor = engine_tensors(request)[0]
    data = tensor.data.materialize()
    assert data.shape == (FRAME_COUNT, 3)
    assert data.dtype == np.float64
    for frame in range(FRAME_COUNT):
        assert np.array_equal(data[frame], make_frame(frame)[gy, gx])
    assert tensor.attributes["representation"] == "roi_t"


def test_temporal_reduce_keeps_data_separate_from_preview(test_video: Path) -> None:
    gx, gy = GREEN_POS
    data_tensor, preview_tensor = engine_reduce(test_video, "mean", rect=(gx, gy, 1, 1))
    data = data_tensor.data.materialize()
    preview = preview_tensor.data.materialize()
    assert data.shape == (1, 1, 3) and data.dtype == np.float64
    assert preview.shape == (1, 1, 3) and preview.dtype == np.uint8
    assert data_tensor.tensor_id != preview_tensor.tensor_id
    assert data_tensor.attributes["artifact_role"] == "data"
    assert preview_tensor.attributes["artifact_role"] == "preview"
    assert data[0, 0, 1] == 255.0


def test_flow_exposes_float_vectors_and_derived_preview(motion_video: Path) -> None:
    pytest.importorskip("cv2")
    tensors = engine_tensors(flow_request(motion_video, frame_a=0, frame_b=1))
    flow_tensor, magnitude_tensor = tensors[0], tensors[-3]
    flow_preview = tensors[-2]
    flow = flow_tensor.data.materialize()
    magnitude = magnitude_tensor.data.materialize()
    assert flow.shape == (64, 64, 2) and flow.dtype == np.float32
    assert magnitude.shape == (64, 64) and magnitude.dtype == np.float32
    assert [channel.name for channel in flow_tensor.channels] == [
        "flow_x", "flow_y",
    ]
    assert flow_preview.attributes["artifact_role"] == "preview"
    assert np.allclose(magnitude, np.hypot(flow[..., 0], flow[..., 1]))


def test_npy_handle_supports_exact_partial_reads(tmp_path: Path) -> None:
    source = np.arange(5 * 7 * 3, dtype=np.float32).reshape(5, 7, 3)
    path = tmp_path / "精确数据.npy"
    handle = save_npy(source, path)
    assert isinstance(handle, NpyArrayHandle)
    assert handle.shape == source.shape
    assert np.array_equal(
        handle.read((slice(1, 4), slice(2, 6), 1)),
        source[1:4, 2:6, 1],
    )
    with pytest.raises(FileExistsError):
        save_npy(source, path)
    handle.close()
    with pytest.raises(ValueError):
        handle.materialize()
    replacement = source + 1
    replaced = save_npy(replacement, path, overwrite=True)
    assert np.array_equal(replaced.materialize(), replacement)
    replaced.close()
    assert not list(tmp_path.glob(".*.tmp"))


def test_frequency_data_keeps_complex_values_and_units(blink_video: Path) -> None:
    temporal_data, temporal_preview = engine_tensors(temporal_spectrum_request(
        blink_video, source="luma", rect=None, point=None,
    ))
    assert temporal_data.data.materialize().dtype == np.complex128
    assert temporal_data.axes[0].unit == "hertz"
    assert temporal_preview.data.dtype == "uint8"

    spatial_data, spatial_preview = engine_tensors(spatial_spectrum_request(
        blink_video, frame=0, time=None, rect=None,
    ))
    assert spatial_data.data.materialize().dtype == np.complex128
    assert [axis.unit for axis in spatial_data.axes] == [
        "cycle/pixel", "cycle/pixel",
    ]
    assert spatial_preview.attributes["artifact_role"] == "preview"


def test_vfr_fft_is_explicitly_marked_as_estimate(vfr_video: Path) -> None:
    data, _ = engine_tensors(temporal_spectrum_request(
        vfr_video, source="luma", rect=None, point=None,
    ))
    assert data.accuracy.level.value == "estimated"
    assert data.attributes["requires_explicit_resampling"] is True
