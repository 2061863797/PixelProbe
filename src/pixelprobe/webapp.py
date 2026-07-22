"""PixelProbe 本地 Web 服务：HTTP API + 可视化 GUI。

仅面向本机使用（默认绑定 127.0.0.1），基于标准库 http.server，
不引入 Web 框架依赖。API 全部只读，包装 pixelprobe.core。

端点（均为 GET）：
- /                    GUI 单页应用
- /api/info            媒体信息 JSON
- /api/frame-times     从 0 开始的真实逐帧 PTS JSON
- /api/media           原始视频流（支持 HTTP Range，供浏览器连续播放）
- /api/frame.png       帧图 PNG（path, frame|time, crop, max_dim）
- /api/pixels          像素查询 JSON（points=x,y|x,y）
- /api/region          区域统计 JSON（rect=x,y,w,h）
- /api/timeline        时间线 JSON + image_base64（points|grid...）
- /api/xt /api/yt      时空切片 JSON + image_base64（coordinate=...）
- /api/changes         变化检测 JSON（point|rect|grid, top）

成功返回 {"success": true, "data": ...}；
失败返回 {"success": false, "error": {code, message}}，
HTTP 状态码按错误类型映射（文件不存在 404，其余业务错误 400）。
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import io
import json
import math
import mimetypes
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image as PILImage

from pixelprobe import core
from pixelprobe.models.errors import (
    InvalidRangeError,
    MediaNotFoundError,
    PixelProbeError,
)
from pixelprobe.output.image_writer import fit_within, scale_nearest
from pixelprobe.utils.coordinates import parse_point, parse_rect
from pixelprobe.utils.validation import ensure_file_exists
from pixelprobe.version import __version__

DEFAULT_PORT = 8799
DEFAULT_MAX_DIM = 1024
MAX_PREVIEW_DIM = 4096
MAX_TOP_CHANGES = 100
MEDIA_CHUNK_SIZE = 1024 * 1024


def _parse_http_range(value: str | None, size: int) -> tuple[int, int] | None:
    """解析单段 HTTP bytes Range，返回含首尾的字节区间。"""
    if value is None:
        return None
    if not value.startswith("bytes=") or "," in value or size <= 0:
        raise ValueError("不支持的 Range")
    spec = value[6:].strip()
    if "-" not in spec:
        raise ValueError("Range 格式错误")
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError("后缀长度必须大于 0")
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            if start < 0 or start >= size or end < start:
                raise ValueError("Range 超出文件范围")
            end = min(end, size - 1)
    except ValueError as exc:
        raise ValueError("Range 格式错误") from exc
    return start, end


def _png_bytes(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    PILImage.fromarray(np.ascontiguousarray(arr)).save(buf, format="PNG")
    return buf.getvalue()


def _png_base64(arr: np.ndarray) -> str:
    return base64.b64encode(_png_bytes(arr)).decode("ascii")


def _auto_scale(arr: np.ndarray, target: int = 512) -> tuple[np.ndarray, int]:
    """小图整数倍最近邻放大到可视尺寸，返回 (数组, 倍数)。"""
    longest = max(arr.shape[0], arr.shape[1])
    if longest >= target:
        return arr, 1
    scale = min(16, max(1, target // longest))
    return scale_nearest(arr, scale, scale), scale


class _Query:
    """查询参数解析助手：缺参/格式错误统一抛 InvalidRangeError。"""

    def __init__(self, raw: dict[str, list[str]]) -> None:
        self._raw = raw

    def get(self, name: str, default: str | None = None) -> str | None:
        values = self._raw.get(name)
        return values[0] if values else default

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None or value == "":
            raise InvalidRangeError(f"缺少必需参数：{name}")
        return value

    def get_int(
        self,
        name: str,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int | None:
        value = self.get(name)
        if value is None or value == "":
            return None
        try:
            parsed = int(value)
        except ValueError as exc:
            raise InvalidRangeError(f"参数 {name}={value!r} 不是整数") from exc
        if minimum is not None and parsed < minimum:
            raise InvalidRangeError(f"参数 {name}={parsed} 必须 >= {minimum}")
        if maximum is not None and parsed > maximum:
            raise InvalidRangeError(f"参数 {name}={parsed} 必须 <= {maximum}")
        return parsed

    def get_float(
        self,
        name: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float | None:
        value = self.get(name)
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
        except ValueError as exc:
            raise InvalidRangeError(f"参数 {name}={value!r} 不是数字") from exc
        if not math.isfinite(parsed):
            raise InvalidRangeError(f"参数 {name}={value!r} 必须是有限数字")
        if minimum is not None and parsed < minimum:
            raise InvalidRangeError(f"参数 {name}={parsed} 必须 >= {minimum}")
        if maximum is not None and parsed > maximum:
            raise InvalidRangeError(f"参数 {name}={parsed} 必须 <= {maximum}")
        return parsed


def _range_kwargs(q: _Query) -> dict:
    sample_every = q.get_int("sample_every", minimum=1)
    return {
        "start_frame": q.get_int("start_frame", minimum=0),
        "end_frame": q.get_int("end_frame", minimum=0),
        "start": q.get_float("start", minimum=0),
        "end": q.get_float("end", minimum=0),
        "sample_every": 1 if sample_every is None else sample_every,
    }


# ---------- 各端点实现（返回 (json_dict) 或 ("png", bytes)） ----------


def _api_info(q: _Query) -> dict:
    return core.get_media_info(Path(q.require("path"))).model_dump()


def _api_frame_times(q: _Query) -> dict:
    """返回真实逐帧 PTS，供浏览器在 VFR 视频中精确映射帧号。"""
    path = ensure_file_exists(Path(q.require("path")))
    if core.detect_media_type(path) != "video":
        raise InvalidRangeError("逐帧时间戳端点仅支持视频文件")
    with core.VideoReader() as reader:
        reader.open(path)
        times = reader.frame_timestamps()
    return {"frame_count": len(times), "times": times}


def _api_frame_png(q: _Query) -> bytes:
    crop_text = q.get("crop")
    arr, _idx, _t, _info = core.get_frame(
        Path(q.require("path")),
        frame=q.get_int("frame", minimum=0),
        time=q.get_float("time", minimum=0),
        crop=parse_rect(crop_text) if crop_text else None,
    )
    requested_max = q.get_int(
        "max_dim", minimum=1, maximum=MAX_PREVIEW_DIM
    )
    max_dim = DEFAULT_MAX_DIM if requested_max is None else requested_max
    return _png_bytes(fit_within(arr, max_dim, max_dim))


def _api_pixels(q: _Query) -> dict:
    points = [parse_point(p) for p in q.require("points").split("|")]
    arr, idx, t, _info = core.load_frame(
        Path(q.require("path")),
        frame=q.get_int("frame", minimum=0),
        time=q.get_float("time", minimum=0),
    )
    samples = core.inspect_pixels(arr, points, frame=idx, time_seconds=t)
    return {
        "frame": idx,
        "time_seconds": t,
        "pixels": [s.model_dump() for s in samples],
    }


def _api_region(q: _Query) -> dict:
    arr, idx, t, _info = core.load_frame(
        Path(q.require("path")),
        frame=q.get_int("frame", minimum=0),
        time=q.get_float("time", minimum=0),
    )
    stats = core.analyze_region(arr, parse_rect(q.require("rect")))
    return {
        "frame": idx,
        "time_seconds": t,
        "statistics": stats.model_dump(),
    }


def _api_timeline(q: _Query) -> dict:
    points_text = q.get("points")
    grid_text = q.get("grid")
    result = core.extract_timelines(
        Path(q.require("path")),
        points=(
            [parse_point(p) for p in points_text.split("|")]
            if points_text else None
        ),
        grid=parse_rect(grid_text) if grid_text else None,
        step=q.get_int("step", minimum=1),
        block_size=q.get_int("block_size", minimum=1),
        **_range_kwargs(q),
    )
    display, scale = _auto_scale(result.matrix, target=256)
    return {
        "k_points": len(result.points),
        "t_frames": len(result.frames),
        "points": [p.model_dump() for p in result.points],
        "frames": result.frames,
        "times": result.times,
        "sample_type": result.sample_type,
        "display_scale": scale,
        "image_base64": _png_base64(display),
    }


def _api_slice(q: _Query, slice_type: str) -> dict:
    coordinate = q.get_int("coordinate", minimum=0)
    if coordinate is None:
        raise InvalidRangeError("缺少必需参数：coordinate")
    fn = core.create_xt_slice if slice_type == "xt" else core.create_yt_slice
    result = fn(Path(q.require("path")), coordinate, **_range_kwargs(q))
    display, scale = _auto_scale(result.array, target=256)
    return {
        "slice_type": slice_type,
        "fixed_coordinate": result.fixed_coordinate,
        "frames": result.frames,
        "times": result.times,
        "raw_width": int(result.array.shape[1]),
        "raw_height": int(result.array.shape[0]),
        "display_scale": scale,
        "image_base64": _png_base64(display),
    }


def _api_changes(q: _Query) -> dict:
    point_text = q.get("point")
    rect_text = q.get("rect")
    grid_text = q.get("grid")
    result = core.detect_changes(
        Path(q.require("path")),
        point=parse_point(point_text) if point_text else None,
        rect=parse_rect(rect_text) if rect_text else None,
        grid=parse_rect(grid_text) if grid_text else None,
        step=q.get_int("step", minimum=1),
        **_range_kwargs(q),
    )
    requested_top = q.get_int("top", minimum=1, maximum=MAX_TOP_CHANGES)
    top = core.top_changes(
        result.records, 10 if requested_top is None else requested_top
    )
    return {
        "mode": result.mode,
        "frames_analyzed": result.frames_analyzed,
        "start_frame": result.frame_range.start,
        "end_frame": result.frame_range.end,
        "records": [r.to_dict() for r in result.records],
        "top": [r.to_dict() for r in top],
    }


_JSON_ROUTES = {
    "/api/info": _api_info,
    "/api/frame-times": _api_frame_times,
    "/api/pixels": _api_pixels,
    "/api/region": _api_region,
    "/api/timeline": _api_timeline,
    "/api/xt": lambda q: _api_slice(q, "xt"),
    "/api/yt": lambda q: _api_slice(q, "yt"),
    "/api/changes": _api_changes,
}


class PixelProbeHandler(BaseHTTPRequestHandler):
    """请求分发：/api/* 走核心层，其余返回 GUI 页面。"""

    server_version = f"PixelProbe/{__version__}"

    def log_message(self, fmt: str, *args: object) -> None:  # 静默访问日志
        pass

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(
            status,
            "application/json; charset=utf-8",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def _serve_media(self, q: _Query, head_only: bool = False) -> None:
        """分块返回本地媒体文件，支持浏览器 seek 所需的单段 Range。"""
        path = ensure_file_exists(Path(q.require("path")))
        if core.detect_media_type(path) != "video":
            raise InvalidRangeError("媒体流端点仅支持视频文件")
        size = path.stat().st_size
        try:
            byte_range = _parse_http_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        start, end = byte_range if byte_range is not None else (0, size - 1)
        status = HTTPStatus.PARTIAL_CONTENT if byte_range is not None else HTTPStatus.OK
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=0")
        if byte_range is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return

        remaining = end - start + 1
        with path.open("rb") as fh:
            fh.seek(start)
            while remaining:
                chunk = fh.read(min(MEDIA_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_HEAD(self) -> None:  # noqa: N802（http.server 命名约定）
        parsed = urlparse(self.path)
        if parsed.path == "/api/media":
            try:
                self._serve_media(_Query(parse_qs(parsed.query)), head_only=True)
            except PixelProbeError as exc:
                status = (
                    HTTPStatus.NOT_FOUND
                    if isinstance(exc, MediaNotFoundError)
                    else HTTPStatus.BAD_REQUEST
                )
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802（http.server 命名约定）
        parsed = urlparse(self.path)
        route = parsed.path
        q = _Query(parse_qs(parsed.query))
        try:
            if route == "/" or route == "/index.html":
                html = (
                    resources.files("pixelprobe") / "static" / "index.html"
                ).read_bytes()
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", html)
            elif route == "/api/media":
                self._serve_media(q)
            elif route == "/api/frame.png":
                self._send(
                    HTTPStatus.OK, "image/png", _api_frame_png(q)
                )
            elif route in _JSON_ROUTES:
                data = _JSON_ROUTES[route](q)
                self._send_json(HTTPStatus.OK, {"success": True, "data": data})
            else:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"success": False,
                     "error": {"code": "NOT_FOUND",
                               "message": f"未知路径：{route}"}},
                )
        except PixelProbeError as exc:
            status = (
                HTTPStatus.NOT_FOUND
                if isinstance(exc, MediaNotFoundError)
                else HTTPStatus.BAD_REQUEST
            )
            self._send_json(
                status, {"success": False, "error": exc.to_dict()}
            )
        except (ConnectionAbortedError, BrokenPipeError):
            pass  # 客户端中断
        except Exception as exc:  # 未预期错误 → 500
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"success": False,
                 "error": {"code": "RUNTIME_ERROR", "message": str(exc)}},
            )


def _validate_loopback_host(host: str) -> None:
    """Web API 可读取本机媒体，只允许监听回环地址。"""
    normalized = host.strip().lower()
    if normalized == "localhost":
        return
    try:
        if ipaddress.ip_address(normalized).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError(
        f"拒绝监听非回环地址 {host!r}；PixelProbe Web 仅允许本机访问"
    )


def serve(host: str, port: int) -> ThreadingHTTPServer:
    """创建并返回 HTTP 服务器（不阻塞，供测试使用）。"""
    _validate_loopback_host(host)
    return ThreadingHTTPServer((host, port), PixelProbeHandler)


def main() -> None:
    """console_scripts 入口：pixelprobe-web。"""
    parser = argparse.ArgumentParser(
        prog="pixelprobe-web",
        description="PixelProbe 本地可视化界面与 HTTP API",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址（默认 127.0.0.1，仅本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器")
    args = parser.parse_args()

    try:
        server = serve(args.host, args.port)
    except ValueError as exc:
        parser.error(str(exc))
    url = f"http://{args.host}:{args.port}/"
    print(f"PixelProbe Web 已启动：{url}（Ctrl+C 停止）")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
