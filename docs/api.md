# PixelProbe Python SDK / API 参考

PixelProbe 的核心分析层可直接作为 Python 库使用，不依赖 Typer/Rich。
CLI、MCP Server 和 Web GUI 都只是这一层的薄包装。

```python
from pixelprobe.core import (
    get_media_info, get_frame, load_frame, inspect_pixels, analyze_region,
    extract_timelines, create_xt_slice, create_yt_slice,
    detect_changes, top_changes, VideoReader, ImageReader,
)
```

安装：`pip install -e .`（开发依赖 `.[dev]`，MCP 依赖 `.[mcp]`）。

## 统一约定

- 坐标原点在左上角，`0 <= x < width`，`0 <= y < height`；
- `pixel_id = y * width + x`；
- 帧号从 0 开始，帧范围为闭区间；帧范围与秒范围不能混用；
- 所有公开时间均相对媒体首帧从 0 开始，不暴露容器内部 PTS 偏移；
- 帧数组统一为 `numpy.ndarray[height, width, 3]`、`uint8`、RGB；
- 所有业务错误继承 `pixelprobe.models.PixelProbeError`（见「错误类型」）。

## 顶层函数

### get_media_info(path) -> MediaInfo

读取图片或视频元数据。`MediaInfo` 字段：`path、media_type("image"|"video")、
width、height、channels、fps、frame_count、frame_count_estimated、
duration_seconds、codec、pixel_format、color_mode、is_vfr、time_base、
file_size_bytes`。

`frame_count_estimated=True` 表示帧数来自 duration×fps 估算（如 MKV 元数据
缺失）；需要精确计数时用 `VideoReader.build_pts_index()`。

### get_frame(path, frame=None, time=None, crop=None) -> (arr, idx, t, info)

取一帧（图片则为图片本身）。`frame`/`time` 二选一（视频默认第 0 帧）；
`crop=(x, y, w, h)` 裁剪。返回 `(RGB 数组, 帧号|None, 时间秒|None, MediaInfo)`。
`load_frame` 与之相同但不含 crop。

按时间取帧规则：**时间戳不大于目标时间的最后一帧**。

### inspect_pixels(frame_array, points, frame=None, time_seconds=None) -> list[PixelSample]

在已解码帧上读取像素。`points` 为 `[(x, y), ...]`。`PixelSample` 字段：
`x、y、pixel_id、frame、time_seconds、time_ms、rgb{r,g,b}、hex、
hsv{h:0-360, s:0-100, v:0-100}、lab{l,a,b}（CIELAB/D65）、
luminance（加权近似，0-255）、luminance_linear（sRGB 线性化后，0-255）`。

### analyze_region(frame_array, rect) -> RegionStatistics

矩形区域统计：`rect、pixel_count、mean/median/min/max/std_rgb、mean_hsv、
mean_lab、mean_luminance、std_luminance`。注意 `mean_hsv.h` 为算术平均
（未做圆周平均）。

### extract_timelines(path, points=None, pixel_ids=None, grid=None, step=None, block_size=None, start_frame=None, end_frame=None, start=None, end=None, sample_every=1, sort="selection", progress=None) -> TimelineResult

多像素时间线，**视频只解码一次**。`points` 与 `grid=(x,y,w,h)` 二选一；
`block_size=N` 为像素块模式（每个采样位置取 N×N 块平均，边界裁剪）。
`sort`: `selection|pixel-id|yx|xy`。`progress` 为 `callable(done, total)`。

返回 `TimelineResult`：`matrix[K,T,3] uint8`、`points: list[PixelCoordinate]`、
`frames: list[int]`、`times: list[float]`、`frame_range`、`sample_type`、
`block_size`、`sort`、`width`、`height`。

### create_xt_slice(path, y, ...) / create_yt_slice(path, x, ...) -> SpacetimeResult

时空切片，范围参数同上。返回 `SpacetimeResult`：`array[T, 空间, 3]`
（xt 空间=宽，yt 空间=高；第 0 行为范围内最早帧）、`slice_type`、
`fixed_coordinate`、`frames`、`times`、`frame_range`。

### detect_changes(path, point=None, rect=None, grid=None, step=None, ..., progress=None) -> ChangesResult

相邻（采样）帧变化量，`point/rect/grid` 最多指定一个，都不给时为
**full 模式（整帧）**。得分定义：
单像素 = 通道绝对差之和（0～765，归一化 /765）；
区域/网格/整帧 = 平均绝对差（0～255，归一化 /255）。
返回 `ChangesResult`：`mode`、`records: list[ChangeRecord]`（按帧升序）、
`frame_range`、`frames_analyzed`。`top_changes(records, n)` 取得分降序
（并列时帧号小者在前）的前 n 条。

### segment_events(records, threshold=None, min_gap=1, min_records=1) -> (list[ChangeEvent], float)

把变化记录按阈值合并为事件区间。阈值作用于 `normalized_score`，
缺省自动取 `mean + 3*std`；相邻超阈记录下标间隔 <= `min_gap` 时并入
同一事件。返回 `(事件列表, 实际使用的阈值)`。`ChangeEvent`：
`start_frame`（变化前最后一帧）、`end_frame`、`start_time/end_time`、
`peak_frame/peak_score/peak_normalized`、`mean_normalized`、`record_count`。

### temporal_reduce(path, op="std", rect=None, ..., p_low=1.0, p_high=99.0, destripe=False, smooth=0, max_median_bytes=1GB, progress=None) -> TemporalReduceResult

时间域合成：把帧序列折叠为一张逐像素统计图，**视频只解码一次**。
`op`: `mean|median|min|max|std|diff`（diff=相邻采样帧绝对差均值，即运动
能量）。除 median 外全部流式聚合（内存 O(H*W)）；median 需持有全部采样帧，
超过 `max_median_bytes`（估算含工作拷贝）抛 `InvalidRangeError`。
`destripe` 扣除逐列/逐行均值（抑制条纹伪影），`smooth=N` 做 N×N 邻域均值
（N>=2 生效）——两者只影响可视化图像。
返回 `TemporalReduceResult`：`image[H,W,3] uint8`（按 `p_low/p_high`
百分位拉伸）、`stat_min/stat_max/stat_mean`（拉伸前每通道摘要）、
`stretch_low_value/stretch_high_value`、`frame_range`、`frames_analyzed`。

### compare_frames(path, frame_a|time_a, frame_b|time_b, rect=None, threshold=10, colormap="fire") -> CompareResult

任意两帧比较（各自帧号/秒二选一）。差异按每像素三通道绝对差的最大值
衡量，超过 `threshold` 计为变化像素。返回 `CompareResult`：
`diff_image[H,W,3]`（按最大差拉伸后伪彩，`gray|fire`）、`mean_abs_diff`、
`max_abs_diff`、`changed_pixels/changed_ratio`、`bbox`（超阈像素外接矩形，
原始分辨率坐标，无变化为 None）。

### sample_frames(path, count=9, cols=None, ..., tile_max_dim=320, annotate=True, progress=None) -> ContactSheetResult

等距抽 `count` 帧拼成网格图（contact sheet），每格底部标注
`f=帧号 t=秒`（ASCII）。目标帧稀疏时用 PTS seek 逐帧取，稠密时单遍
顺序解码。`plan_sheet_frames(frame_range, count)` 与
`compose_sheet(tiles, frames, times, ...)` 为可独立复用的纯函数。
返回 `ContactSheetResult`：`image`、`frames`、`times`、`cols/rows`、
`tile_width/tile_height`。

### scan_media(path, sheet_count=9, sample_every=None, event_threshold=None, tile_max_dim=320, progress=None) -> ScanResult

一键概览扫描，**单遍解码**同时完成：整帧变化曲线、等距抽帧网格、
亮度异常帧检测（black/white/flat/flash）。`sample_every=None` 时自动
降采样（全片约 1800 帧封顶）。返回 `ScanResult`：`info`、`sheet`、
`records`、`events`、`event_threshold`、`anomalies`（最多 200 条，
超出置 `anomalies_truncated`）、`effective_sample_every`、`frames_analyzed`。

### temporal_spectrum(path, source="luma", rect=None, point=None, ..., progress=None) -> TemporalSpectrumResult

时间域 FFT 周期检测。`source`: `luma`（区域平均亮度序列）或 `change`
（相邻帧变化量序列）；`rect` 与 `point` 最多给一个。采样率与 VFR 判定
基于解码得到的真实帧时间戳（帧间隔波动 >10% 置 `vfr_warning`）。
至少需要 8 个采样值。返回：`dominant_freq_hz/period_seconds/period_frames`
（序列平坦时为 None）、`peak_ratio`、`top_peaks`（幅度并列偏向低频）、
`spectrum_image`、`effective_fps`、`samples`。

### spatial_spectrum(path, frame=None, time=None, rect=None) -> SpatialSpectrumResult

单帧二维 FFT，检测条纹/摩尔纹/周期纹理（区域至少 8×8）。返回中心化
log 幅度谱图与 `peaks`（屏蔽中心低频、按共轭对称去重）：每项含
`u/v`（相对中心的频率坐标）、`period_px`（条纹周期）、`angle_deg`
（频率向量方向，条纹走向与其垂直）、`magnitude`。

### compute_flow(path, frame_a|time_a, frame_b|time_b, ..., accumulate=False, compensate_global=False, mag_threshold=1.0, progress=None) -> FlowResult

Farneback 稠密光流。**需要可选依赖**：`pip install "pixelprobe[flow]"`，
未安装时抛 `DependencyMissingError`（模块可正常导入，仅调用时报错）。
两帧模式与累积模式（`accumulate=True`，改用帧范围参数，逐对光流相加、
单遍解码）二选一。`compensate_global=True` 时从稠密流网格采样估计全局
仿射（平移/旋转/缩放）并逐像素扣除。返回 `FlowResult`：
`flow_image`（HSV 方向着色）、`magnitude_image`（幅度伪彩）、
`mean/max/p95_magnitude`、`dominant_angle_deg`（0°=向右，y 向下为正）、
`global_motion`（dx/dy/rotation_deg/scale/matrix）、`motion_bbox`、
`frames_analyzed`。

## VideoReader（底层接口）

```python
from pixelprobe.core import VideoReader

with VideoReader() as reader:
    reader.open(path)
    info = reader.get_info()
    t, arr = reader.get_frame_by_index(120)
    idx, t, arr = reader.get_frame_by_time(4.0)
    for idx, t, arr in reader.iter_frames(0, 299, sample_every=10):
        ...
    index = reader.build_pts_index()   # 精确帧表（demux 不解码）
    times = reader.frame_timestamps()  # 从 0 开始的逐帧秒数
```

寻址策略：元数据可信的恒定帧率视频用 fps 公式 seek（快）；VFR、
元数据缺帧数或索引已构建时用 **PTS 索引**（精确，CFR/VFR 通用）；
两者失效时自动从头顺序解码，保证帧号一致。

## 颜色工具（pixelprobe.core.color）

`rgb_to_hex(r,g,b)`、`rgb_to_hsv_array(arr)`、`rgb_to_lab_array(arr)`
（CIELAB/D65）、`luminance_array(arr)`（加权近似）、
`luminance_linear_array(arr)`（sRGB 反伽马线性化）、`srgb_to_linear(arr)`。
均为向量化实现，输入 `[..., 3] uint8`。

## 错误类型（pixelprobe.models）

| 异常 | code | CLI 退出码 |
| --- | --- | --- |
| PixelProbeError（基类） | PIXELPROBE_ERROR | 1 |
| InvalidRangeError | INVALID_RANGE | 2 |
| MediaNotFoundError | FILE_NOT_FOUND | 3 |
| UnsupportedMediaError | UNSUPPORTED_MEDIA | 4 |
| CoordinateOutOfRangeError | COORDINATE_OUT_OF_RANGE | 5 |
| FrameOutOfRangeError / TimeOutOfRangeError | FRAME/TIME_OUT_OF_RANGE | 6 |
| DecodeError | DECODE_FAILED | 7 |
| OutputWriteError | OUTPUT_WRITE_FAILED | 8 |
| DependencyMissingError | DEPENDENCY_MISSING | 1 |

每个异常有 `message`、可选 `hint` 和 `to_dict()`。

## 输出工具（pixelprobe.output）

- `image_writer.save_png(arr, path)`：原子写 PNG；
  `scale_nearest(arr, sx, sy)`：最近邻整数放大；
  `fit_within(arr, max_w, max_h)`：保持宽高比缩小（NEAREST）；
- `plot.render_curve(values, width=768, height=256, markers=None, spans=None, y_min=None, y_max=None)`：
  纯 numpy/PIL 折线图（横轴=下标，spans 为背景描色的下标闭区间）；
  `plot.apply_colormap(gray, name="fire")`：`[H,W]` uint8 灰度 → 伪彩
  `[H,W,3]`（`gray|fire`）；
- `csv_writer.write_timeline_csv / write_changes_csv`；
- `json_writer.dump_success / dump_error`：CLI JSON 信封。

## 三种集成方式

| 入口 | 命令 | 适用 |
| --- | --- | --- |
| CLI | `pixelprobe <cmd> --json` | 脚本、任意语言子进程调用 |
| MCP | `pixelprobe-mcp`（stdio） | Claude 等 AI Agent 工具调用 |
| HTTP + GUI | `pixelprobe-web`（仅回环地址，默认 127.0.0.1:8799） | 本机浏览器界面 / 本机程序调用 |

HTTP API 端点与参数见 `src/pixelprobe/webapp.py` 模块 docstring；其中
`/api/frame-times` 返回从 0 开始的真实逐帧 PTS，供 VFR GUI 精确同步。
返回 `{"success": true, "data": ...}` 或 `{"success": false, "error": {...}}`。
