"""V0.7 Operator、统一数值 Tensor、Path/ROI 与 NPY 验收测试。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from conftest import FRAME_COUNT, GREEN_POS, RED_Y, make_frame
from pixelprobe import core
from pixelprobe.artifacts import NpyArrayHandle, save_npy
from pixelprobe.operators.base import OperatorSpec
from pixelprobe.operators.sampling import SamplingConfig, resample_polyline


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


def test_xt_legacy_result_is_backed_by_normative_tensor(test_video: Path) -> None:
    result = core.create_xt_slice(test_video, RED_Y)
    assert [axis.name for axis in result.tensor.axes] == ["time", "x", "channel"]
    assert result.tensor.data.dtype == "uint8"
    assert np.array_equal(result.array, result.tensor.data.materialize())
    assert result.tensor.attributes["representation"] == "xt"


def test_timeline_compatibility_only_transposes_new_tensor(test_video: Path) -> None:
    result = core.extract_timelines(test_video, points=[(0, RED_Y), (1, RED_Y)])
    tensor = result.tensor.data.materialize()
    assert tensor.shape == (FRAME_COUNT, 2, 3)
    assert [axis.name for axis in result.tensor.axes] == ["time", "path", "channel"]
    assert np.array_equal(result.matrix, np.transpose(tensor, (1, 0, 2)))


def test_path_t_matches_xt_on_integer_horizontal_path(test_video: Path) -> None:
    path = core.create_path_t(
        test_video,
        [(0.0, float(RED_Y)), (31.0, float(RED_Y))],
        sample_count=32,
        interpolation="nearest",
    )
    xt = core.create_xt_slice(test_video, RED_Y)
    assert path.tensor.data.shape == (FRAME_COUNT, 32, 3)
    assert np.array_equal(path.tensor.data.materialize(), xt.array)
    path_mapping = next(
        mapping for mapping in path.tensor.axis_mappings
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
    result = core.create_roi_t(
        test_video,
        (gx, gy, 1, 1),
        reduction="mean",
    )
    data = result.tensor.data.materialize()
    assert data.shape == (FRAME_COUNT, 3)
    assert data.dtype == np.float64
    for frame in range(FRAME_COUNT):
        assert np.array_equal(data[frame], make_frame(frame)[gy, gx])
    assert result.tensor.attributes["representation"] == "roi_t"


def test_temporal_reduce_keeps_data_separate_from_preview(test_video: Path) -> None:
    gx, gy = GREEN_POS
    result = core.temporal_reduce(test_video, op="mean", rect=(gx, gy, 1, 1))
    data = result.data_tensor.data.materialize()
    preview = result.preview_tensor.data.materialize()
    assert data.shape == (1, 1, 3) and data.dtype == np.float64
    assert preview.shape == (1, 1, 3) and preview.dtype == np.uint8
    assert result.data_tensor.tensor_id != result.preview_tensor.tensor_id
    assert result.data_tensor.attributes["artifact_role"] == "data"
    assert result.preview_tensor.attributes["artifact_role"] == "preview"
    assert data[0, 0, 1] == 255.0


def test_flow_exposes_float_vectors_and_derived_preview(motion_video: Path) -> None:
    pytest.importorskip("cv2")
    result = core.compute_flow(motion_video, frame_a=0, frame_b=1)
    flow = result.flow_tensor.data.materialize()
    magnitude = result.magnitude_tensor.data.materialize()
    assert flow.shape == (64, 64, 2) and flow.dtype == np.float32
    assert magnitude.shape == (64, 64) and magnitude.dtype == np.float32
    assert [channel.name for channel in result.flow_tensor.channels] == [
        "flow_x", "flow_y",
    ]
    assert result.flow_tensor is result.raw_flow_tensor
    assert result.flow_preview_tensor.attributes["artifact_role"] == "preview"
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
    temporal = core.temporal_spectrum(blink_video, source="luma")
    temporal_data = temporal.data_tensor.data.materialize()
    assert temporal_data.dtype == np.complex128
    assert temporal.data_tensor.axes[0].unit == "hertz"
    assert temporal.preview_tensor.data.dtype == "uint8"

    spatial = core.spatial_spectrum(blink_video, frame=0)
    spatial_data = spatial.data_tensor.data.materialize()
    assert spatial_data.dtype == np.complex128
    assert [axis.unit for axis in spatial.data_tensor.axes] == [
        "cycle/pixel", "cycle/pixel",
    ]
    assert spatial.preview_tensor.attributes["artifact_role"] == "preview"


def test_vfr_fft_is_explicitly_marked_as_compatibility_estimate(
    vfr_video: Path,
) -> None:
    result = core.temporal_spectrum(vfr_video, source="luma")
    assert result.data_tensor.accuracy.level.value == "estimated"
    assert result.data_tensor.attributes["requires_explicit_resampling"] is True
