---
name: pixelprobe-video-analysis
description: 用 PixelProbe MCP 工具搭配 AI 原生视觉能力分析本地视频/图片。当需要分析视频内容、定位事件时刻、寻找噪声中的隐藏图案/水印、检测周期闪烁或条纹、分析运动方向、或需要精确到帧号/坐标/像素值的结论时使用。
---

# PixelProbe 视频分析方法论

PixelProbe 是 AI 原生视觉理解的**精确数据辅助工具**，不是替代品。分工原则：

- **你的视觉能力负责语义**：画面里是什么对象、发生了什么事件、因果与上下文。
- **PixelProbe 负责数值**：精确帧号、真实时间戳、原始分辨率坐标、像素值，以及"单帧看不见、只有跨帧统计才显形"的内容。
- **任何数值发现都要回到原始帧视觉确认**：像素变化 ≠ 对象移动，变化峰值 ≠ 事件发生，频谱峰 ≠ 语义结论。对候选时刻至少查看发生前、候选帧、发生后三帧。

## 场景 → 工具

| 问题 | 入口 | 后续 |
| --- | --- | --- |
| 不了解这个视频 | `scan_media`（一次调用出信息+网格+事件+异常帧） | 按发现分流 |
| 快速浏览画面 | `sample_frames` | `extract_frame` 看单帧细节 |
| 什么时候发生了变化 | `detect_changes`（缺省整帧，自动事件分段） | `compare_frames` 定位区域 → `extract_frame` 确认 |
| 两帧之间哪里变了 | `compare_frames`（bbox+占比） | `extract_frame` + crop 放大看 |
| 单帧看不见的内容（噪声藏图、水印、坏点） | `temporal_reduce` | 见下方专题 |
| 运动发生在画面哪里 | `temporal_reduce(op=diff)` | `xt_slice`/`yt_slice` 看轨迹形态 |
| 周期闪烁 / 频闪 / 周期噪声 | `temporal_spectrum` | 按周期帧数抽帧对比 |
| 条纹 / 摩尔纹 / 重复纹理 | `spatial_spectrum` | — |
| 运动方向、镜头运动 vs 物体运动 | `optical_flow`（`compensate_global=true` 扣镜头） | `extract_frame` 确认对象 |
| 固定点颜色随时间变化 | `extract_timeline` | — |
| 精确读数 | `inspect_pixels` / `analyze_region` | — |

## 专题：噪声中的隐藏内容（temporal_reduce）

原理：背景噪点逐帧重新随机，隐藏图案区域的噪点时间变化更慢 → 逐像素时间标准差在图案区域显著偏低。

推荐调用：`op=std, destripe=true, smooth=5~11`。

- `destripe`：噪声生成器常带逐列/逐行条纹伪影，不扣掉会盖住图案。
- `smooth`：N×N 邻域均值，压制噪声粒度、凸显区域结构；5~11 之间试，太大糊掉细节。
- 百分位拉伸保持默认（p_low=1, p_high=99）；**拉到 p5–p95 会接近二值化**，反而破坏可读性。
- 判读：图中**暗 = 统计值低 = 更静止**（可能是图案），亮 = 变化剧烈。精确数值以 JSON 元数据的 stat_*/stretch_* 为准，不要靠图目测。
- 图案仍模糊时：用 `rect` 聚焦候选区域重算（等效放大信噪比），或加大 `smooth`。
- `op=median` 需一次持有全部帧，超内存会报错并提示 `sample_every`/`rect`；`op=min/max` 适合找固定叠加物和坏点。

## 判读速查

- **temporal_reduce 图**：亮=统计值高。std 图暗区=时间上更静止；diff 图亮区=运动能量集中处。
- **optical_flow 流场图**：hue=运动方向（0°=向右，y 向下为正），亮度=速度。幅度伪彩图亮=快。`dominant_angle_deg` 是运动区域内幅度加权方向，为 null 表示无显著运动或方向相互抵消（如两块反向运动）。`global_motion` 的 dx/dy/rotation/scale 是镜头运动估计。
- **temporal_spectrum**：主频为 None=序列平坦无周期；频率一律按实测平均帧间隔换算，`vfr_warning=true` 表示帧间隔波动大、均值不可靠仅供参考。`peak_ratio` 低说明周期性弱。
- **X-T/Y-T 切片**：斜线=匀速运动，竖直条带=静止物，水平横条=全画面事件（闪光/切镜头）。
- **detect_changes 事件**：`start_frame` 是变化前最后一帧；`peak_frame` 适合与 `previous_frame` 做 `compare_frames`。

## 实用参数经验

- 长视频一律加 `sample_every` 降采样（`scan_media` 会自动，约 1800 帧封顶）。**例外：用 `temporal_spectrum` 检测周期闪烁时慎用**——高于 `nyquist_hz`（有效采样率一半）的闪烁会混叠成假频率或完全漏采，优先 `sample_every=1` 并用帧范围缩小时间窗。
- 所有坐标基于原始分辨率、原点左上、帧号从 0 起、范围闭区间、时间单位秒。
- 返回图像可能被缩放（看 `display_scale`/`returned_width`），精确像素值永远用 `inspect_pixels`。
- 只有用户明确要文件时才调用 `save_*` 工具。
- 光流需要 `pip install "pixelprobe[flow]"`；缺依赖会返回 `DEPENDENCY_MISSING`，其余功能不受影响。

## 参考案例：噪点视频还原隐藏文字

一个 6 秒 1280×720 视频，逐帧全是黑白雪花，暂停/抽帧均无内容。流程：
1. `get_media_info` → 确认 177 帧、无 VFR；
2. `sample_frames` → 确认所有代表帧都是纯噪声（排除单帧内容）；
3. `temporal_reduce(op=std, destripe=true, smooth=11)` → 统计图中部浮现一行暗色字符「12345」；
4. 结论表述：以统计图为证据、以字符形状的视觉辨认为判断，标注帧范围与区域坐标。
