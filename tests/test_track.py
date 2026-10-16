"""点跟踪：精确轨迹、跟丢判定、跟随切片与入口。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pixelprobe.address import parse_address
from pixelprobe.capabilities import capability
from pixelprobe.models.errors import AddressError
from test_find import _write_video


@pytest.fixture(scope="module")
def moving_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """静止随机背景上，12×12 纹理块每帧右移 3、下移 1；第 8 帧起跳到 40 像素外。"""
    rng = np.random.default_rng(7)
    background = rng.integers(0, 120, (96, 96, 3), dtype=np.uint8)
    block = rng.integers(130, 256, (12, 12, 3), dtype=np.uint8)
    frames = []
    for number in range(12):
        x, y = 10 + 3 * number, 20 + number
        if number >= 8:
            x += 40
        frame = background.copy()
        frame[y: y + 12, x: x + 12] = block
        frames.append(frame)
    return _write_video(tmp_path_factory.mktemp("track") / "moving.mkv", frames)


def _track(media: Path, at: str, **options):
    outcome = capability("track").invoke(media, parse_address(at), options)
    return outcome


def test_track_follows_block_exactly_and_stops_when_lost(moving_video: Path) -> None:
    outcome = _track(moving_video, "point(16,26)", patch=4)
    data = outcome.data
    assert [(p["x"], p["y"]) for p in data["track"]] == [(16 + 3 * n, 26 + n) for n in range(8)]
    assert data["track"][1]["dx"] == 3 and data["track"][1]["dy"] == 1
    assert data["track"][1]["cost"] == 0.0 and data["track"][0]["cost"] is None
    assert data["lost_at"] == 8 and data["end_frame"] == 7 and data["requested_end_frame"] == 11
    assert data["displacement"] == {"dx": 21, "dy": 7}
    assert data["path_length_pixels"] == pytest.approx(7 * np.hypot(3, 1), abs=1e-3)
    assert data["mean_speed_pixels_per_second"] == pytest.approx(7 * np.hypot(3, 1) / 0.28, rel=1e-3)
    assert outcome.refs == {"track_end": "f=7 point(37,33)"}
    assert outcome.warnings and "跟丢" in outcome.warnings[0]
    strip = outcome.images["follow"]()
    assert strip.shape == (8, 64, 3)
    # 跟随切片中线始终是纹理块上同一位置的像素
    assert all((strip[row, 32] == strip[0, 32]).all() for row in range(8))
    path = outcome.images["path"]()
    assert path.shape == (96, 96, 3)


def test_wider_search_keeps_tracking_and_range_is_respected(moving_video: Path) -> None:
    data = _track(moving_video, "f=6..11 point(34,32)", patch=4, search=48).data
    assert data["lost_at"] is None and data["frames"] == [6, 7, 8, 9, 10, 11]
    assert (data["track"][2]["x"], data["track"][2]["y"]) == (16 + 24 + 40, 34)


def test_track_requires_one_point_and_no_step(moving_video: Path) -> None:
    with pytest.raises(AddressError):
        _track(moving_video, "f=0..5")
    with pytest.raises(AddressError):
        _track(moving_video, "point(1,1) point(2,2)")
    with pytest.raises(AddressError):
        _track(moving_video, "step=2 point(16,26)")


def test_cli_track(moving_video: Path, tmp_path: Path) -> None:
    from conftest import run_json
    follow = tmp_path / "follow.png"
    data = run_json("track", str(moving_video), "--at", "f=0..5 point(16,26)", "--patch", "4",
                    "--follow-output", str(follow), "--json")["data"]
    assert data["end_point"] == {"x": 31, "y": 31} and follow.is_file()
    assert data["refs"]["track_end"] == "f=5 point(31,31)"
