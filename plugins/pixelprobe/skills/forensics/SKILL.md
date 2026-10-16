---
name: forensics
description: Use when examining whether a local image or video may have been edited, re-encoded, or lacks provenance — reading EXIF, checking C2PA content credentials, ELA/noise/edge inconsistency maps for images, or container tags, keyframe structure and timestamp irregularities for videos. Uses the PixelProbe MCP tools and reports reviewable clues with limitations, never an authenticity verdict.
---

# 图片与视频取证线索

PixelProbe **不判定真伪**。所有输出都是可复核的线索、参数与局限；结论必须由人结合来源和上下文作出。
汇报时逐条写明"看到了什么、它可能的普通解释、它不能证明什么"。

## 图片

`pixelprobe_evidence media_path=图片`（`kind=auto` 按扩展名选择）：
- EXIF 字段名（完整内容在结果包里）、C2PA 状态：区分"没有凭证 / 验证失败 / 信任未知 / 可信"，
  离线验证，签名有效不等于内容真实；
- ELA、噪声一致性、边缘候选热力图写入结果包：`pixelprobe_bundle action=list/read` 读正式数值，
  预览图只用于定位；
- PNG 等无损格式的 ELA 标记为不适用，不能当作"没有问题"。
- 透明或调色板图片：`pixelprobe_overview` 会说明存储通道与显示通道的差异，原生样本用
  `pixelprobe_measure what=pixel options={"sample":"native"}`。

## 视频

`pixelprobe_evidence media_path=视频`（自动选 `kind=streams`；`options={"frame_types":true}` 再统计 I/P/B）：
- 容器与流标签（封装软件、creation_time 等）——可被任意改写，封装器标签不等于重新编码；
- 关键帧间隔是否固定、B 帧推断、时间戳回退/重复/空隙、每秒字节；
- `clues` 每条附证据；GOP 不规则可能来自场景自适应关键帧、推流或拼接，
  对照 `pixelprobe_find query="cut"` 看关键帧是否落在镜头切换处。

## 比对两个版本

`pixelprobe_compare media_path=疑似版本 reference_path=原始版本`：逐帧差异、首个差异帧与变化区域，
结果写入结果包；要求尺寸相同，不做内容或时间对齐。
