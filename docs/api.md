# PixelProbe Python 使用说明

PixelProbe 除了命令行，也可以在 Python 脚本中读取媒体、生成确定性数值表示和
保存可复现 Bundle。

```python
from pixelprobe.core import (
    get_media_info, get_frame, load_frame, load_native_image, inspect_pixels,
    inspect_native_pixels, analyze_region,
    extract_timelines, create_xt_slice, create_yt_slice,
    detect_changes, top_changes,
)
```

安装请使用 README 中的仓库克隆命令；若需可复现的直接安装，请固定到发布标签或提交哈希。

## 统一生成 API

`pixelprobe.generate(request)` 接受一个 `RepresentationRequest` 或请求元组，
返回正式 Data Tensor、可选 Preview、ExecutionPlan 和结构化执行事件。请求可生成
X-T、Y-T、点/Path/ROI 时间表示、灰度/HSV/Lab、Frame Difference、Reduction、
FFT/STFT 和 Farneback 光流。同一媒体的多个请求共享一次解码。

需要保存结果时传入 `output_path`，并把请求的 `output.format` 设为 `bundle` 或
`zarr`。Bundle 会保存数值、坐标索引、映射、来源身份、ExecutionPlan、事件、
provenance 和 SHA-256；Preview 不会替代 Data。

## 统一约定

- 坐标原点在左上角，`0 <= x < width`，`0 <= y < height`；
- `pixel_id = y * width + x`；
- 帧号从 0 开始，帧范围为闭区间；帧范围与秒范围不能混用；
- 所有公开时间均相对媒体首帧从 0 开始，不暴露容器内部 PTS 偏移；
- `load_frame()` 与旧帧类接口返回显示 RGB8：`numpy.ndarray[height, width, 3]`、`uint8`；
  图片需读取原生通道、Alpha、调色板索引或高位深值时使用 `load_native_image()`；
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

### load_native_image(path) -> (array, NativeImageMetadata, MediaInfo)

读取图片的 Pillow 原生样本数组，不做 RGB8 显示转换。`NativeImageMetadata` 提供
`mode、source_format、dtype、shape、bands、bits_per_sample、has_alpha、
alpha_representation、sample_semantics`。明确识别的 PNG/BMP/GIF/PNM 常见无损格式标为
`stored_sample`；JPEG 等有损或无法确认的格式标为 `decoded_sample`。

### inspect_native_pixels(image_array, points, bands, sample_semantics) -> list[dict]

读取 `load_native_image()` 的每个原生通道值。返回 `channels、values、dtype` 和
`sample_semantics`，不擅自套用 RGB、HSV、Lab 或亮度计算；调色板图的 `values` 是调色板索引。

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
缺省自动取"剔除最大记录后的 `mean + 3*std`"（避免小样本时尖峰抬高
自身阈值）；相邻超阈记录下标间隔 <= `min_gap` 时并入
同一事件。返回 `(事件列表, 实际使用的阈值)`。`ChangeEvent`：
`start_frame`（变化前最后一帧）、`end_frame`、`start_time/end_time`、
`peak_frame/peak_time/peak_score/peak_normalized`、`mean_normalized`、
`record_count`。

### temporal_reduce(path, op="std", rect=None, ..., p_low=1.0, p_high=99.0, destripe=False, smooth=0, max_median_bytes=1GB, progress=None) -> TemporalReduceResult

时间域合成：把帧序列折叠为一张逐像素统计图，**视频只解码一次**。
`op`: `mean|median|min|max|std|diff`（diff=相邻采样帧绝对差均值，即运动
能量）。除 median 外全部流式聚合（内存 O(H*W)）；median 需持有全部采样帧，
超过 `max_median_bytes`（估算含工作拷贝）抛 `InvalidRangeError`。
`destripe` 做双向去趋势（抑制条纹伪影；显示统计量变为零中心残差：
0=符合行列趋势，负=更静止），`smooth=N` 做 N×N 邻域均值（N>=2 生效）
——两者只影响可视化图像，`stat_*` 始终是原始统计量摘要。
返回 `TemporalReduceResult`：`image[H,W,3] uint8`（按 `p_low/p_high`
百分位拉伸）、`stat_min/stat_max/stat_mean`（拉伸前每通道摘要）、
`stretch_low_value/stretch_high_value` 与 `stretch_domain`（端点所在
数值空间：`raw` / `detrended_residual` / `smoothed`，可用 `+` 组合；
destripe 启用后端点为残差空间数值，可为负）、`frame_range`、
`frames_analyzed`。

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
（序列平坦时为 None）、`peak_ratio`、`top_peaks`（幅度并列偏向低频；
偶数样本时 Nyquist bin 幅度已折半以与其他 bin 可比）、
`spectrum_image`、`effective_fps`、`nyquist_hz`（可检测频率上限；
`sample_every > 1` 时更高频率的成分会混叠或漏采）、`samples`。

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
`mean/max/p95_magnitude`、`dominant_angle_deg`（0°=向右，y 向下为正；
运动区域内幅度加权方向，无运动或方向相互抵消时为 None）、
`global_motion`（dx/dy/rotation_deg/scale/matrix）、`motion_bbox`、
`frames_analyzed`。

## VideoReader（底层接口）

```python
from pixelprobe.core.video_reader import VideoReader

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

## CLI 集成

外部程序统一通过 `pixelprobe <cmd> --json` 调用。标准输出只包含一个 JSON
对象，成功与失败分别使用 `{"success": true, "data": ...}` 和
`{"success": false, "error": {...}}` 信封；诊断信息写入标准错误。
