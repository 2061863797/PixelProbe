"""审核发现的精确取帧与重复解码回归测试。"""
from unittest.mock import patch
import pytest
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.core.contact_sheet import sample_frames
from pixelprobe.models.errors import FrameOutOfRangeError


def test_estimated_count_must_not_hide_real_tail(test_video):
    with VideoReader() as reader:
        reader.open(test_video)
        frames = list(reader.iter_frames(0, None))
        last = frames[-1][0]
        with patch.object(reader, "frame_count", return_value=(last, True)):
            assert reader.get_frame_by_index(last)[1].shape == frames[-1][2].shape
            with pytest.raises(FrameOutOfRangeError):
                reader.get_frame_by_index(last + 1)


def test_sparse_sheet_scans_once(test_video):
    original = VideoReader.iter_frames
    calls = []
    def counted(self, *args, **kwargs):
        calls.append(args)
        yield from original(self, *args, **kwargs)
    with patch.object(VideoReader, "iter_frames", counted):
        result = sample_frames(test_video, count=8)
    assert len(result.frames) == 8
    assert len(calls) == 1


def test_unified_mcp_entry(monkeypatch):
    import sys
    from pixelprobe.cli.app import main
    import pixelprobe_mcp.entry as entry
    calls = []
    monkeypatch.setattr(sys, "argv", ["pixelprobe", "--mcp"])
    monkeypatch.setattr(entry, "main", lambda: calls.append(True))
    main()
    assert calls == [True]
