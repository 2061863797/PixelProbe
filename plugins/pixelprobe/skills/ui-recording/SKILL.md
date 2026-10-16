---
name: ui-recording
description: Use when analyzing a screen recording or UI screenshots of software — measuring animation or transition durations, finding dropped frames, stutter or frozen UI, checking when a control changes, comparing two screenshots pixel by pixel, or reading exact UI colors and positions. Uses the PixelProbe MCP tools.
---

# 录屏与 UI 分析

先按 pixelprobe-core 的打法了解视频（`pixelprobe_overview`），再用下面的配方。
录屏常是可变帧率：时间以返回的 `time_seconds` 为准，不要用帧号 ÷ 帧率推算。

## 动画 / 过渡时长

1. `pixelprobe_look view=frame grid=true` 找到控件所在格子或矩形。
2. `pixelprobe_find query="animation in rect(x,y,w,h)"`（可加 `at="t=2s..6s"` 限定范围）。
   每个结果给出开始帧、停稳帧、`duration_ms`、`settled` 和精度 `frame_interval_seconds`。
3. 压缩噪声被当成"在动"时调高 `options.animation_tolerance`；缓动尾巴被切开时调大 `options.settle_seconds`。

## 卡顿、掉帧、冻结

- `query="gap"`：相邻帧时间间隔超过中位间隔 1.5 倍（掉帧/卡顿候选），`value` 是倍数。
- `query="duplicate"`：与上一帧逐像素相同的重复帧（编码端补帧、渲染没出新帧）。
- `query="freeze >= 0.5s"`：整帧停住；整帧平均会稀释光标这类小变化，局部是否卡住用
  `query="change in rect(...)"` 或 `"animation in rect(...)"`。
- 怀疑时间戳本身有问题时：`pixelprobe_evidence`（视频自动给出码流结构，含时间戳空隙与回退）。

## 何时变化、变成什么

- `query="first change in cell(B2)"` 找某区域第一次变化；再 `pixelprobe_look view=frame at="@match1"` 看画面。
- 两帧对比：`pixelprobe_compare at="f=100,101 rect(...)"` 给差异热力图、变化像素数、bbox。
- 两张截图或两个版本：`pixelprobe_compare media_path=新 reference_path=旧`。

## 颜色与位置

- `pixelprobe_measure what=pixel at="f=120 point(x,y)"`：RGB/HEX/HSV/Lab；
  `what=region at="f=120 rect(...)"` 区域统计；
  `what=track at="f=10..80 point(x,y)"` 跟踪光标或控件的移动路径与速度（`images=true` 附轨迹图）。
