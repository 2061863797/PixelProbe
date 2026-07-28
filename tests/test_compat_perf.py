"""兼容性与性能约束测试：常见 H.264+yuv420p 视频（容差断言）、单次解码。"""

from __future__ import annotations

from pathlib import Path

from conftest import run_json
from pixelprobe import core


def test_compat_h264_yuv420p_tolerance(compat_video: Path) -> None:
    """常见 MP4（yuv420p 有损）：只做容差断言。"""
    info = core.get_media_info(compat_video)
    assert info.media_type == "video"
    assert info.pixel_format == "yuv420p"
    # 每帧纯色灰阶 20 + t*20，允许 ±6 容差
    for t in (0, 4, 9):
        arr, idx, _, _ = core.get_frame(compat_video, frame=t)
        assert idx == t
        expected = 20 + t * 20
        assert abs(int(arr[16, 16, 0]) - expected) <= 6
        assert abs(int(arr[16, 16, 1]) - expected) <= 6


def test_compat_pixel_cli(compat_video: Path) -> None:
    data = run_json(
        "pixel", compat_video, "--frame", 9, "--point", "8,8", "--json"
    )["data"]
    rgb = data["pixels"][0]["rgb"]
    assert abs(rgb["r"] - 200) <= 6


def test_timeline_decodes_video_once(test_video: Path, monkeypatch) -> None:
    """多点提取只解码视频一次。

    通过统计规范解码入口 VideoReader.iter_frame_packets 的调用次数验证。
    """
    import pixelprobe.core.video_reader as vr_module

    calls = {"n": 0}
    original = vr_module.VideoReader.iter_frame_packets

    def counting_iter(self, *args, **kwargs):
        calls["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(vr_module.VideoReader, "iter_frame_packets", counting_iter)
    result = core.extract_timelines(
        test_video,
        grid=(0, 0, 32, 32),
        step=2,  # 256 个采样点
    )
    assert result.matrix.shape[0] == 256
    assert calls["n"] == 1, (
        f"应只解码一遍，实际 iter_frame_packets 调用 {calls['n']} 次"
    )


def test_video_reader_reuses_color_converter(test_video: Path, monkeypatch) -> None:
    """顺序分析应复用颜色转换器，不能为每一帧重复初始化。"""
    import pixelprobe.core.video_reader as vr_module

    original = vr_module.VideoReformatter
    calls = {"instances": 0, "frames": 0}

    class CountingReformatter:
        def __init__(self) -> None:
            calls["instances"] += 1
            self._delegate = original()

        def reformat(self, *args, **kwargs):
            calls["frames"] += 1
            return self._delegate.reformat(*args, **kwargs)

    monkeypatch.setattr(vr_module, "VideoReformatter", CountingReformatter)
    with vr_module.VideoReader() as reader:
        reader.open(test_video)
        frames = list(reader.iter_frames(0, 5))

    assert len(frames) == 6
    assert calls == {"instances": 1, "frames": 6}
