# 地址写法（--at）

从 2.0 起，所有命令都用同一个参数 `--at` 指定"分析哪段时间、画面哪个位置"，
取代旧的 `--frame/--time/--start/--end/--start-frame/--end-frame/--sample-every/
--point/--pixel-id/--rect/--crop/--grid/--step/--x/--y/--frame-a/--frame-b/...`。
控制"怎么算"的参数（如 `--op`、`--threshold`、`--block-size`、`--output`）不变。

地址由空格分隔的若干项组成，**顺序随意、大小写不敏感**，整体用引号括起来：

```bash
pixelprobe region video.mp4 --at "f=120 rect(400,200,200,150)"
pixelprobe changes video.mp4 --at "t=1s..5s step=2 cell(C4)"
```

## 时间

| 写法 | 含义 |
|---|---|
| `f=120` | 第 120 帧（帧号从 0 开始） |
| `f=100..200` | 第 100～200 帧（两端都包含） |
| `f=100..` / `f=..200` | 从第 100 帧到结尾 / 从开头到第 200 帧 |
| `f=0,5` | 两帧：第 0 帧和第 5 帧（`compare`、`flow` 使用） |
| `f=all` 或不写 | 全部帧（单帧命令不写时默认第 0 帧） |
| `t=3.5s`、`t=3.5`、`t=3500ms`、`t=1:02.5`、`t=1:00:02` | 时间点；对应"不晚于该时刻的最后一帧" |
| `t=12s..15s`、`t=0s,0.5s`、`t=all` | 时间范围、两个时间点、全部 |

所有时间都从媒体首帧算起，从 0 开始。

## 采样

| 写法 | 含义 |
|---|---|
| `step=3` | 时间上每隔 3 帧取 1 帧 |

## 空间（坐标原点在左上角，x 向右、y 向下，单位为像素）

| 写法 | 含义 |
|---|---|
| `point(520,340)` | 一个像素；可以写多个 |
| `pid(1000)` | 按编号指定像素：`pid = y × 宽度 + x` |
| `rect(x,y,宽,高)` | 矩形区域 |
| `grid(x,y,宽,高 step=4)` | 矩形内每隔 4 像素取一个采样点 |
| `row(120)` / `col(40)` | 一整行 / 一整列（X–T、Y–T 切片使用） |
| `cell(C4)` | 网格中的一格：默认把画面分成 8 列（A～H）× 6 行（1～6） |
| `cell(B2:C3)` | 跨多格的矩形区域 |
| `cell(C4, 4x4)` | 指定网格规格：4 列 × 4 行 |
| `line(x1,y1 -> x2,y2)`、`path(x,y -> x,y -> ...)` | 线段、折线（供后续 Path–T 等功能使用） |

`cell` 的格子边界按 `floor(序号 × 画面尺寸 / 格数)` 划分，所有格子无缝拼满整幅画面。
它让 AI 看着带网格的画面时可以说"C4 那一格"，而不必估计像素坐标。

## 引用（@名称）

`@名称` 会被替换成引用表里对应的一段地址，可以和其他项组合：

```bash
pixelprobe pixel video.mp4 --at "@flash point(0,0)" --ref-file refs.json
```

`--ref-file` 接受三种 JSON：`{"refs": {"flash": "f=15"}}`、命令行输出信封
`{"success": true, "data": {"refs": {...}}}`，或扁平映射 `{"flash": "f=15"}`。
同一项（比如时间）被引用和直接写法同时指定时会报错，不会悄悄覆盖。

## 各命令接受的写法

| 命令 | 时间 | 空间 | step |
|---|---|---|---|
| `frame` | 单帧 | `rect`、`cell`（裁剪） | — |
| `pixel` | 单帧 | `point`、`pid`（至少一个） | — |
| `region` | 单帧 | `rect` 或 `cell`（必填） | — |
| `timeline` | 单帧、范围 | 多个 `point`/`pid`，或一个 `grid` | ✓ |
| `xt` / `yt` | 单帧、范围 | `row(y)` / `col(x)`（必填） | ✓ |
| `changes` | 单帧、范围 | 一个 `point`/`pid`/`rect`/`cell`/`grid`，不写为整帧 | ✓ |
| `find` | 范围（缺省全片） | —（位置写在搜索语句里，见 [条件搜索](find.md)） | — |
| `reduce` | 范围 | `rect`、`cell` | ✓ |
| `compare` | 两帧（必填） | `rect`、`cell` | — |
| `flow` | 两帧；`--accumulate` 时为范围 | — | 累积时 ✓ |
| `sheet` | 范围 | — | — |
| `scan` | —（全片） | — | ✓ |
| `spectrum` | 范围 | 一个 `point`/`pid`/`rect`/`cell` | ✓ |
| `spectrum2d` | 单帧 | `rect`、`cell` | — |
| `workflow` | 帧号范围（缺省 0～1799） | — | ✓ |

写法不符合命令要求时，错误信息会说明"这个命令可以怎么写"，并以错误码
`INVALID_ADDRESS`（退出码 2）返回。

## 从 1.x 迁移

| 1.x | 2.0 |
|---|---|
| `--frame 120` | `--at "f=120"` |
| `--time 3.5` | `--at "t=3.5s"` |
| `--start-frame 10 --end-frame 50 --sample-every 2` | `--at "f=10..50 step=2"` |
| `--start 1 --end 3` | `--at "t=1s..3s"` |
| `--point 1,2 --point 3,4` | `--at "point(1,2) point(3,4)"` |
| `--pixel-id 100` | `--at "pid(100)"` |
| `--rect 0,0,8,8`、`--crop 0,0,8,8` | `--at "rect(0,0,8,8)"` |
| `--grid 0,0,32,32 --step 4` | `--at "grid(0,0,32,32 step=4)"` |
| `xt --y 120` / `yt --x 40` | `xt --at "row(120)"` / `yt --at "col(40)"` |
| `--frame-a 0 --frame-b 5` | `--at "f=0,5"` |
| `scan --sample-every 5` | `scan --at "step=5"` |
