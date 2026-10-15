# 条件搜索（find）

`find` 用一句搜索语句在视频里定位时刻与区间，回答"哪里黑屏、哪里闪了一下、哪里卡住、
哪一帧开始变化"这类问题。结果是**按固定数值定义判定的候选**，不理解画面内容；
定位后请用 `sheet` / `frame`（MCP 的 `pixelprobe_look`）查看画面再下结论。

```powershell
pixelprobe find "录屏.mp4" "black | flash | freeze >= 0.5s"
pixelprobe find "录屏.mp4" "first change in cell(B2)" --at "t=10s..20s" --json
pixelprobe find "片子.mkv" "similar f=1200" --json
```

MCP：`pixelprobe_find`，传入 `query`（`at` 只写时间范围）。

## 写法

一条语句由一个或多个条件用 `|` 连接，结果取并集；关键字不区分大小写。

| 条件 | 含义 |
|---|---|
| `black` / `white` / `flat` | 黑帧、白帧、纯色帧（与 `scan` 相同：最大值 < 16 / 最小值 > 240 / 标准差 < 2） |
| `flash` | 闪帧：恰好连续两次跳变，且前后两帧相同（中间那帧被短暂替换） |
| `cut` | 镜头切换：连续一到两次跳变且不是闪帧；区间为切换后的第一帧 |
| `duplicate` | 与上一帧逐像素完全相同的重复帧 |
| `freeze >= 0.5s`、`freeze >= 15f` | 画面停住至少多久（`s`、`ms` 或 `f`=帧；缺省 0.5s） |
| `gap`、`gap >= 2x` | 相邻帧时间间隔超过中位间隔的倍数（缺省 1.5x）：掉帧、卡顿候选 |
| `similar f=120`、`similar t=3.5s <= 4` | 与参考帧外观相近（差分哈希距离，缺省 <= 6） |
| `frame.luma > 200` | 整帧指标比较 |
| `rect(0,0,50,50).r < 30`、`cell(C4).diff > 20` | 区域指标比较（point / pid / rect / cell） |
| `change in cell(B2)`、`first change in rect(...) > 12` | 区域变化事件；`first` 只取第一个 |
| `animation in rect(...)`、`animation in cell(C4)` | 区域动画时长：从第一个变化帧到最终画面首次出现 |

指标：`luma`（Rec.709 权重亮度）、`r` / `g` / `b`（通道均值）、`std` / `min` / `max`
（三通道合计）、`diff`（与上一帧的平均绝对差，0～255）；整帧另有 `sharpness`
（缩到长边 256 后拉普拉斯方差，只用于同一视频内相对比较，模糊帧数值低）和 `blockiness`
（8 像素块边界处的亮度相邻差 ÷ 块内部相邻差：约 1 表示没有块效应，明显大于 1 表示压缩方块突出；
块内部完全平坦时无值、不参与比较）。例如 `frame.blockiness > 1.5`、`frame.sharpness < 20`。比较符：`> >= < <= == !=`。

**跳变**：与上一帧的平均绝对差 / 255 ≥ `jump_threshold`（缺省 0.1，即约 25.5）。
采用固定阈值（参考 PySceneDetect 的做法），不随片段统计变化；连续三次以上跳变视为渐变或运动，
不算切换。`--jump-threshold` / `options.jump_threshold` 可调。

**停住**：与上一帧的整帧平均绝对差 ≤ `freeze_tolerance`（缺省 0.1）。整帧平均会稀释小面积变化：
大画面中移动的光标可能仍被算作"停住"。判断局部是否停住请用 `change in <区域>`。

**动画**：区域内与上一帧的平均绝对差 > `animation_tolerance`（缺省 1.0）的帧算"在动"；
中间静止不足 `settle_seconds`（缺省 0.2 秒，CLI `--settle`）的变化合并为同一次动画（缓动、
中途停顿一两帧都算同一次）。`value` / `duration_ms` 是第一个变化帧与最终画面首帧的显示时间差，
精度为一个帧间隔（`frame_interval_seconds`）；`settled=false` 表示到范围末尾仍未静止满
`settle_seconds`。测 UI 动画请尽量用无损或高码率录屏，压缩噪声会被当作"在动"，必要时调高容差。

```powershell
pixelprobe find "录屏.mp4" "animation in rect(120,40,200,48)" --at "t=2s..6s"
```

**相似**：64 位差分哈希只反映亮度起伏的结构；纯色画面之间的距离都为 0。

## 结果

每个结果是一个帧区间：`kind`、`start_frame` / `end_frame`、`start_time` / `end_time`、
`frame_count`、`value`（区间内最能说明条件的值：大于类取最大、小于类取最小；freeze 为时长秒，
gap 为倍数，similar 为最小距离）及各条件的附加说明。结果编号 `match1`、`match2`… 写成
`f=a..b`，可直接用于后续地址（`--at "@match2" --ref-file 上次输出.json`，MCP 中直接写 `@match2`）。
`definitions` 字段给出本次用到的每个条件的完整数值定义。

## 数据来源与一致性

- 整帧条件使用每帧体检值（与媒体索引 L1 同一函数计算）。已建索引且完整覆盖所选范围时直接查表
  （`computed_from: "index"`），否则顺序解码一遍现算；两条路径逐值相同（有回归测试）。
- 范围起点之前多解码一帧，使第一帧的 `diff` 也有前帧；整片从第 0 帧开始时第一帧没有前帧。
- 区域条件必须读取原始像素，总是解码。MCP 中无索引时最多顺序解码 3600 帧；长视频请先
  `pixelprobe index build`，或用 `--at` 缩小范围。
- 只适用于视频；图片请用 `pixel` / `region`。
