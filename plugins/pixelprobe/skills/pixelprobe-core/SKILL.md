---
name: pixelprobe-core
description: Use when a task needs exact facts about a local image or video — frame numbers, timestamps, pixel coordinates or colors, region statistics, when something changes, or measurable defects — rather than a description of what the picture shows. Covers the general coarse-to-fine workflow with the PixelProbe MCP tools (overview → find → look → measure) and the shared location syntax.
---

# PixelProbe：通用打法

你自己负责看懂画面内容（人物、物体、文字、场景）；PixelProbe 负责可核实的数字：
帧号、时间、坐标、像素值、变化定位、取证线索。不要凭 PixelProbe 的数值下语义结论，
也不要从缩放后的预览图读像素值。

## 先粗后细

1. `pixelprobe_overview`（缺省 `detail=meta`）：尺寸、帧率、编码；视频有索引时附分段 `seg1…`。
   视频想先看全貌用 `detail=scan`：代表帧网格、变化事件 `event1…`、黑/白/纯色/闪帧候选。
2. `pixelprobe_find`：定位时刻。`query` 写条件，结果编号 `match1…`：
   `"black | flash | cut"`、`"freeze >= 0.5s"`、`"gap"`、`"similar f=120"`、
   `"frame.luma > 200"`、`"first change in cell(B2)"`、`"animation in rect(...)"`。
3. `pixelprobe_look`：带坐标刻度看画面。`view=frame`（单帧）、`sheet`（抽帧网格）、
   `xt`/`yt`（时空切片，`options.source=diff` 突出运动、`options.time_axis=adaptive` 压缩长视频）、
   `reduce`（时间域合成）。`grid=true` 叠加 8×6 网格 A1…H6，格子名可直接写进地址。
4. `pixelprobe_measure`：精确数值。`what=pixel/region/timeline/spectrum/spectrum2d/flow/track`。

## 地址写法（at 参数，与命令行 --at 相同）

- 时间：`f=120`、`f=100..200`、`t=3.5s`、`t=12s..15s`、`f=0,15`（两帧）、`step=5`
- 位置：`point(520,340)`、`rect(x,y,w,h)`、`cell(C4)`、`cell(B2:C3)`、`row(120)`、`col(40)`、`grid(x,y,w,h step=4)`
- 引用：上一步返回的 `refs` 可写成 `@event1`、`@match2`、`@seg3`、`@track_end`，服务器按媒体记住
- 组合：`"t=12s..15s rect(400,200,200,150)"`、`"@event1 cell(C4)"`

坐标原点在左上角，x 向右、y 向下，单位是原始像素；帧号从 0 开始；范围都是闭区间。

## 长视频与后台任务

- 超过约 3600 帧的视频先 `pixelprobe_index action=build wait=false`，用 `pixelprobe_job` 查询；
  建好后整帧条件的 find、changes、scan 直接查表。
- 任何工具都可传 `wait=false` 立即返回 `job_id`；`pixelprobe_job action=get/cancel/result`。
- 不确定某能力接受什么写法时调 `pixelprobe_system`（action=capabilities）。

## 汇报

给出帧号与时间、坐标和数值，说明结果来自哪个工具及其定义（返回里的 `definitions` / `method`）；
候选结果先 `pixelprobe_look` 看过前、中、后画面再下结论。
