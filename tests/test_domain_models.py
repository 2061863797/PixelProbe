"""V0.6 领域语义、兼容转换与帧包验收测试。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from pixelprobe.compat import (
    legacy_frame_range_to_selection,
    selection_to_legacy_frame_range,
)
from pixelprobe.core.frame_selector import FrameRange
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.domain import (
    AccuracyInfo,
    AccuracyLevel,
    AxisKind,
    AxisMapping,
    AxisSpec,
    ChannelSpec,
    CoordinateSpace,
    CoordinateSpaceKind,
    MediaSource,
    MemoryArrayHandle,
    PathGeometry,
    PointGeometry,
    ProvenanceRef,
    RectGeometry,
    TemporalSelection,
    TensorField,
    TensorFieldDescriptor,
)
from pixelprobe.domain.errors import (
    ArraySelectionOutOfRangeError,
    AxisShapeMismatchError,
    MaterializationLimitExceededError,
)


def decoded_accuracy() -> AccuracyInfo:
    return AccuracyInfo(
        level=AccuracyLevel.DECODED,
        source="test_decoder:1",
        unit="code_value",
    )


def test_temporal_selection_uses_half_open_interval() -> None:
    selection = TemporalSelection(
        mode="frame_interval",
        requested_start_frame=2,
        requested_end_frame_exclusive=6,
        sample_every=2,
    )
    assert selection.requested_start_frame == 2
    assert selection.requested_end_frame_exclusive == 6

    with pytest.raises(ValidationError):
        TemporalSelection(
            mode="frame_interval",
            requested_start_frame=2,
            requested_end_frame_exclusive=2,
        )


@pytest.mark.parametrize(
    "factory",
    (
        lambda: PointGeometry(
            coordinate_space_id="storage_pixels", x=float("nan"), y=0,
        ),
        lambda: RectGeometry(
            coordinate_space_id="storage_pixels", x=0, y=0,
            width=float("inf"), height=1,
        ),
        lambda: PathGeometry(
            type="line", coordinate_space_id="storage_pixels",
            points=((0, 0), (float("-inf"), 1)),
        ),
    ),
)
def test_geometry_rejects_non_finite_coordinates(factory) -> None:
    with pytest.raises(ValidationError):
        factory()


def test_legacy_closed_range_round_trip_is_lossless() -> None:
    legacy = FrameRange(start=2, end=8, sample_every=2)
    selection = legacy_frame_range_to_selection(legacy)
    assert selection.requested_end_frame_exclusive == 9
    assert selection_to_legacy_frame_range(selection) == legacy


def test_selection_modes_cannot_be_mixed() -> None:
    with pytest.raises(ValidationError):
        TemporalSelection(
            mode="indices",
            requested_indices=(1, 3),
            requested_start_seconds=0.0,
        )
    with pytest.raises(ValidationError):
        TemporalSelection(mode="indices", requested_indices=(1, 1))


def test_memory_array_handle_never_silently_clips_or_exposes_source() -> None:
    source = np.arange(24, dtype=np.uint8).reshape(2, 4, 3)
    handle = MemoryArrayHandle(source)
    source[:] = 0
    assert handle.materialize()[0, 0, 1] == 1

    part = handle.read((slice(0, 2), slice(1, 4), 2))
    part[:] = 0
    assert handle.materialize()[0, 1, 2] == 5

    with pytest.raises(ArraySelectionOutOfRangeError):
        handle.read((slice(None), slice(0, 5), 0))
    with pytest.raises(MaterializationLimitExceededError):
        handle.materialize(max_bytes=source.nbytes - 1)


def test_tensor_field_enforces_axis_shape_and_channels() -> None:
    accuracy = decoded_accuracy()
    axes = (
        AxisSpec(name="y", kind=AxisKind.Y, length=2, unit="pixel"),
        AxisSpec(name="x", kind=AxisKind.X, length=4, unit="pixel"),
        AxisSpec(name="channel", kind=AxisKind.CHANNEL, length=3),
    )
    channels = tuple(
        ChannelSpec(
            name=name,
            semantic=f"display_srgb_{name}",
            value_range=(0, 255),
            accuracy=accuracy,
        )
        for name in ("r", "g", "b")
    )
    field = TensorField(
        tensor_id="rgb",
        data=MemoryArrayHandle(np.zeros((2, 4, 3), dtype=np.uint8)),
        axes=axes,
        channels=channels,
        coordinate_space=CoordinateSpace(
            coordinate_space_id="storage_pixels",
            kind=CoordinateSpaceKind.STORAGE,
            axes=("x", "y"),
            width=4,
            height=2,
        ),
        axis_mappings=(),
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id="decode"),
        attributes={},
    )
    assert field.data.shape == (2, 4, 3)

    with pytest.raises(AxisShapeMismatchError):
        TensorField(
            tensor_id="bad",
            data=field.data,
            axes=(axes[0], axes[1], axes[2].model_copy(update={"length": 4})),
            channels=channels,
            coordinate_space=field.coordinate_space,
            axis_mappings=(),
            validity=None,
            accuracy=accuracy,
            provenance=field.provenance,
            attributes={},
        )


def test_axis_mapping_and_models_publish_json_schema() -> None:
    mapping = AxisMapping(
        mapping_id="map_time",
        kind="affine",
        input_artifact_id="source",
        input_axes=("time",),
        output_axes=("time",),
        parameters={"scale": 0.04, "offset": 0.0},
        accuracy=AccuracyInfo(
            level=AccuracyLevel.DERIVED,
            source="calculation",
            unit="second",
        ),
    )
    assert mapping.parameters["scale"] == 0.04
    assert "properties" in TemporalSelection.model_json_schema()
    assert "properties" in TensorFieldDescriptor.model_json_schema()

    with pytest.raises(ValidationError):
        MediaSource(
            source_id="sequence",
            kind="image_sequence",
            uri="frames/",
        )


def test_normative_document_examples_match_real_schema() -> None:
    document = (
        Path(__file__).parents[1]
        / "docs"
        / "design"
        / "PixelProbe 核心数据模型设计.md"
    ).read_text(encoding="utf-8")

    valid_section = document.split("## 14. 有效示例", 1)[1]
    valid_json = valid_section.split("```json", 1)[1].split("```", 1)[0]
    descriptor = TensorFieldDescriptor.model_validate(json.loads(valid_json))
    assert descriptor.tensor_id == "tensor_path_t_rgb"
    unsupported = json.loads(valid_json)
    unsupported["schema_version"] = "1.0.0"
    with pytest.raises(ValidationError):
        TensorFieldDescriptor.model_validate(unsupported)


    invalid_section = document.split("## 15. 无效示例", 1)[1]
    invalid_json = invalid_section.split("```json", 1)[1].split("```", 1)[0]
    with pytest.raises(ValidationError):
        TensorFieldDescriptor.model_validate(json.loads(invalid_json))


def test_frame_packet_and_legacy_tuple_are_pixel_identical(vfr_video: Path) -> None:
    with VideoReader() as reader:
        reader.open(vfr_video)
        packets = list(reader.iter_frame_packets(2, 5))
    with VideoReader() as reader:
        reader.open(vfr_video)
        legacy = list(reader.iter_frames(2, 5))

    assert [packet.presentation_index for packet in packets] == [2, 3, 4, 5]
    assert [item[0] for item in legacy] == [2, 3, 4, 5]
    for packet, (index, timestamp, array) in zip(packets, legacy):
        assert packet.presentation_index == index
        assert packet.timeline_time_seconds == timestamp
        assert np.array_equal(packet.data, array)
        assert packet.pts is not None
        assert packet.source_timestamp_seconds == pytest.approx(
            float(packet.pts * packet.time_base)
        )
        assert packet.decoded_pixel_format == "rgb24"
        assert packet.sample_semantics == "decoded_sample"


def test_frame_packet_separates_source_and_normalized_time(
    offset_vfr_video: Path,
) -> None:
    with VideoReader() as reader:
        reader.open(offset_vfr_video)
        packets = list(reader.iter_frame_packets(0, 1))

    assert packets[0].pts is not None and packets[0].pts > 0
    assert packets[0].source_timestamp_seconds is not None
    assert packets[0].source_timestamp_seconds > 0
    assert packets[0].timeline_time_seconds == 0.0
    assert packets[1].timeline_time_seconds > 0.0
