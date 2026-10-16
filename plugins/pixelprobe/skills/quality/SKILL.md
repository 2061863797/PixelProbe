---
name: quality
description: Use when checking a local video or image for technical quality problems — blur, compression blocking, black or blank frames, frozen or duplicated frames, flicker, banding/moire patterns, bitrate drops, or dropped frames — and when locating exactly where they occur. Uses the PixelProbe MCP tools; reports measurable candidates, not subjective quality scores.
---

# 质量与异常检测

PixelProbe 给出可复核的数值候选，不给主观画质评分。先 `pixelprobe_overview detail=scan` 看全貌。

| 问题 | 怎么查 |
|---|---|
| 黑场、白场、纯色帧 | `pixelprobe_find query="black \| white \| flat"` |
| 闪帧 | `query="flash"`；周期性闪烁用 `pixelprobe_measure what=spectrum at="rect(...)"` 看主频 |
| 冻结、重复帧 | `query="freeze >= 0.5s"`、`query="duplicate"` |
| 掉帧、时间戳异常 | `query="gap"`；`pixelprobe_evidence` 的码流结构里看 `timestamp_gaps`、`dts_regressions` |
| 模糊 | `query="frame.sharpness < N"`：sharpness 只适合同一视频内相对比较，先看正常段的数值再定 N |
| 压缩方块 | `query="frame.blockiness > 1.5"`：约 1 表示无块效应，越大越明显 |
| 条纹、摩尔纹、规则纹理 | `pixelprobe_measure what=spectrum2d at="f=120 rect(...)"` 看空间频谱峰 |
| 码率骤降 | `pixelprobe_evidence` 码流结构的 `bytes_per_second` 序列 |
| 质量概览存档 | `pixelprobe_overview detail=quality` 生成可校验结果包 |

## 做法

1. 先用一个"正常"片段测出基准值（例如 `query="frame.sharpness > 0"` 加 `at` 范围，看 value 分布），
   再用阈值条件找异常段，避免拍脑袋设阈值。
2. 对每个候选 `pixelprobe_look view=frame at="@match1" grid=true` 核实；需要局部数值用
   `pixelprobe_measure what=region`。
3. 汇报时给出帧区间、时间、指标值与阈值，并引用返回的 `definitions`。
