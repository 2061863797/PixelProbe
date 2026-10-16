---
name: long-video
description: Use when an AI needs to understand or navigate a long local video — building a map of its shots and segments, finding scene cuts, black or flash frames, locating the moment something happens, or jumping to similar frames — without watching every frame. Uses the PixelProbe MCP tools with the media index.
---

# 长视频辅助

目标是用少量图片和数字建立"视频地图"，再只在关键时刻细看。

## 1. 建索引（一次即可）

`pixelprobe_index action=build media_path=... wait=false`，返回 `job_id` 后用
`pixelprobe_job action=get` 等到 `succeeded`。索引按文件内容识别，文件改动后自动失效。
建好后 `pixelprobe_overview` 会附分段目录 `seg1…`（变化段与平稳段首尾相接覆盖全片）。

## 2. 地图

- `pixelprobe_overview detail=scan`：代表帧网格 + 变化事件 + 异常帧候选。
- `pixelprobe_find query="cut"`：镜头切换（固定跳变阈值，区间是切换后的第一帧）。
- `pixelprobe_find query="black | white | flash"`：片头片尾黑场、闪白、闪帧。
- 一张图看完一个长镜头：`pixelprobe_look view=yt at="col(960)" options={"time_axis":"adaptive","rows":400}`
  ——变化多处取帧密、静止处疏，纵轴刻度是帧号。

## 3. 定位与细看

- 用编号跳转：`pixelprobe_look view=sheet at="@seg3"`、`view=frame at="@match2"`。
- 找"和这一帧相似的画面"（重复镜头、回到同一场景）：`query="similar f=1200"`，
  距离越小越像（纯色画面之间距离都为 0，需结合画面判断）。
- 某区域何时变化：`query="change in cell(D3)"`；整帧亮度条件：`query="frame.luma < 30"`。

## 注意

- 所有结果都是按数值定义判定的候选，`definitions` 字段写明了定义；结论前先看画面。
- 没有索引时 MCP 最多顺序解码 3600 帧；超出时缩小 `at` 的时间范围或先建索引。
