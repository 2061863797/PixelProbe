"""pixelprobe-web HTTP API 测试：本机线程内起服务，urllib 调用。"""

from __future__ import annotations

import base64
import io
import json
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from conftest import FLASH_FRAME, FRAME_COUNT, GREEN_POS, make_frame
from pixelprobe.webapp import serve


@pytest.fixture(scope="module")
def base_url():
    server = serve("127.0.0.1", 0)  # 随机端口
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def _get(base_url: str, route: str, **params: object):
    query = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    req = urllib.request.Request(f"{base_url}{route}?{query}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers.get_content_type(), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get_content_type(), exc.read()


def _get_json(base_url: str, route: str, **params: object) -> dict:
    status, ctype, body = _get(base_url, route, **params)
    assert ctype == "application/json"
    payload = json.loads(body)
    assert payload["success"] is (status == 200)
    return payload


def test_index_page(base_url: str) -> None:
    status, ctype, body = _get(base_url, "/")
    assert status == 200 and ctype == "text/html"
    html = body.decode("utf-8")
    assert "PIXEL" in html
    assert 'api("frame-times"' in html
    assert "seconds * fps" not in html


def test_api_info(base_url: str, test_video: Path) -> None:
    data = _get_json(base_url, "/api/info", path=test_video)["data"]
    assert data["width"] == 32 and data["frame_count"] == FRAME_COUNT


def test_api_frame_times(
    base_url: str, offset_vfr_video: Path,
) -> None:
    data = _get_json(
        base_url, "/api/frame-times", path=offset_vfr_video
    )["data"]
    assert data["frame_count"] == len(data["times"])
    assert data["frame_count"] > 1
    assert data["times"][0] == 0.0
    assert data["times"] == sorted(data["times"])


def test_api_frame_png(base_url: str, test_video: Path) -> None:
    status, ctype, body = _get(
        base_url, "/api/frame.png", path=test_video, frame=FLASH_FRAME
    )
    assert status == 200 and ctype == "image/png"
    arr = np.asarray(Image.open(io.BytesIO(body)).convert("RGB"))
    assert (arr == 255).all()


def test_api_media_full_and_range(base_url: str, test_video: Path) -> None:
    source = test_video.read_bytes()
    encoded = urllib.parse.urlencode({"path": str(test_video)})
    url = f"{base_url}/api/media?{encoded}"

    with urllib.request.urlopen(url) as resp:
        assert resp.status == 200
        assert resp.headers["Accept-Ranges"] == "bytes"
        assert resp.read() == source

    req = urllib.request.Request(url, headers={"Range": "bytes=7-31"})
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 206
        assert resp.headers["Content-Range"] == f"bytes 7-31/{len(source)}"
        assert resp.read() == source[7:32]

    req = urllib.request.Request(url, headers={"Range": "bytes=-16"})
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 206
        assert resp.read() == source[-16:]


def test_api_media_invalid_range(base_url: str, test_video: Path) -> None:
    encoded = urllib.parse.urlencode({"path": str(test_video)})
    req = urllib.request.Request(
        f"{base_url}/api/media?{encoded}",
        headers={"Range": "bytes=999999999-"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 416
    assert exc_info.value.headers["Content-Range"].startswith("bytes */")


def test_api_pixels(base_url: str, test_video: Path) -> None:
    gx, gy = GREEN_POS
    data = _get_json(
        base_url, "/api/pixels", path=test_video, frame=3,
        points=f"{gx},{gy}|0,0",
    )["data"]
    assert data["pixels"][0]["rgb"] == {"r": 0, "g": 255, "b": 0}
    assert "lab" in data["pixels"][0]


def test_api_region(base_url: str, test_video: Path) -> None:
    data = _get_json(
        base_url, "/api/region", path=test_video, frame=15,
        rect="0,0,16,16",
    )["data"]
    assert data["statistics"]["mean_luminance"] == 255.0


def test_api_changes(base_url: str, test_video: Path) -> None:
    data = _get_json(
        base_url, "/api/changes", path=test_video, rect="0,0,32,32", top=1
    )["data"]
    assert data["top"][0]["frame"] == FLASH_FRAME
    assert len(data["records"]) == FRAME_COUNT - 1


def test_api_xt_image(base_url: str, test_video: Path) -> None:
    data = _get_json(
        base_url, "/api/xt", path=test_video, coordinate=8
    )["data"]
    raw = Image.open(io.BytesIO(base64.b64decode(data["image_base64"])))
    arr = np.asarray(raw.convert("RGB"))
    scale = data["display_scale"]
    assert arr.shape == (FRAME_COUNT * scale, 32 * scale, 3)
    # 帧 1 红点 x=1：放大后取块内点验证与原视频一致
    assert tuple(arr[1 * scale, 1 * scale]) == (255, 0, 0)
    expected = make_frame(2)[8, 2]
    assert tuple(arr[2 * scale, 2 * scale]) == tuple(expected)


def test_api_timeline(base_url: str, test_video: Path) -> None:
    gx, gy = GREEN_POS
    data = _get_json(
        base_url, "/api/timeline", path=test_video, points=f"{gx},{gy}"
    )["data"]
    assert data["k_points"] == 1 and data["t_frames"] == FRAME_COUNT


def test_error_mapping(base_url: str, test_video: Path) -> None:
    payload = _get_json(base_url, "/api/info", path="不存在.mp4")
    assert payload["error"]["code"] == "FILE_NOT_FOUND"
    payload = _get_json(
        base_url, "/api/pixels", path=test_video, points="999,999"
    )
    assert payload["error"]["code"] == "COORDINATE_OUT_OF_RANGE"
    status, _, _ = _get(base_url, "/api/nothing")
    assert status == 404


@pytest.mark.parametrize(
    ("route", "params"),
    [
        ("/api/frame.png", {"frame": 0, "max_dim": 0}),
        ("/api/frame.png", {"time": "nan"}),
        ("/api/changes", {"rect": "0,0,32,32", "sample_every": 0}),
        ("/api/changes", {"rect": "0,0,32,32", "top": 0}),
        ("/api/changes", {"rect": "0,0,32,32", "top": -1}),
    ],
)
def test_invalid_numeric_parameters_are_rejected(
    base_url: str, test_video: Path, route: str, params: dict,
) -> None:
    payload = _get_json(base_url, route, path=test_video, **params)
    assert payload["error"]["code"] == "INVALID_RANGE"


def test_serve_rejects_non_loopback_host() -> None:
    with pytest.raises(ValueError, match="仅允许本机访问"):
        serve("0.0.0.0", 0)
