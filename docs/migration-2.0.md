# 从 1.x 迁移到 2.0

2.0 直接切换到新接口：**旧的命令行位置参数和 17 个旧 MCP 工具名都已删除，没有兼容别名**。
已有结果包（Bundle Schema 0.1）的读取与校验保持兼容；计算结果与 1.1.0 逐值一致（38 项数值基线）。

## 一句话总结

| 方面 | 1.x | 2.0 |
|---|---|---|
| 命令行写位置 | `--frame/--time/--start/--end/--point/--rect/...` 多个参数 | 一个 `--at "<地址>"`，见 [地址写法](address.md) |
| MCP | 17 个工具 | 10 个工具，用参数选功能；能力与命令行完全对等 |
| AI 看到的图 | 原图 PNG | 带坐标刻度、可叠加网格、按 `max_side` 缩放并给出换算比例 |
| 引用上次结果 | 抄帧号 | 结果编号，如 `@event1`、`@match2`、`@seg3` |
| 长视频 | 每次重新解码 | 可建媒体索引，整帧问题直接查表 |
| MCP 长任务 | submit / get / result / resume 四步 | 任意工具 `wait=false`，`pixelprobe_job` 查询与领取；不再支持 checkpoint 恢复 |
| 新功能 | — | `find` 搜索语言、`track` 点跟踪、`streams` 码流结构、差分与自适应切片、`index` |

## 命令行

所有分析命令用 `--at` 指定时间与位置，完整对照见 [地址写法 · 从 1.x 迁移](address.md#从-1x-迁移)。常见改法：

| 1.x | 2.0 |
|---|---|
| `pixelprobe frame v.mp4 --frame 120` | `pixelprobe frame v.mp4 --at "f=120"` |
| `pixelprobe pixel v.mp4 --time 3.5 --point 520,340` | `pixelprobe pixel v.mp4 --at "t=3.5s point(520,340)"` |
| `pixelprobe region v.mp4 --frame 15 --rect 4,4,10,10` | `pixelprobe region v.mp4 --at "f=15 rect(4,4,10,10)"` |
| `pixelprobe changes v.mp4 --start 10 --end 20 --rect ...` | `pixelprobe changes v.mp4 --at "t=10s..20s rect(...)"` |
| `pixelprobe xt v.mp4 --y 120 --start-frame 0 --end-frame 300` | `pixelprobe xt v.mp4 --at "f=0..300 row(120)"` |
| `pixelprobe compare v.mp4 --frame-a 100 --frame-b 101` | `pixelprobe compare v.mp4 --at "f=100,101"` |
| `pixelprobe scan v.mp4 --sample-every 5` | `pixelprobe scan v.mp4 --at "step=5"` |

新写法还支持 `cell(C4)`（8×6 网格，与图片上叠加的网格同一定义）和 `@名称` 引用
（命令行用 `--ref-file 上次输出.json` 指定编号来源）。地址写错时错误码为 `INVALID_ADDRESS`（退出码 2）。

新增命令：`find`（[条件搜索](find.md)）、`track` 与切片新选项（[新切法](slices.md)）、
`streams`（[码流结构](streams.md)）、`index build/show/remove/prune`（[媒体索引](media-index.md)）。

## MCP 工具对照

| 1.x 工具 | 2.0 写法 |
|---|---|
| `pixelprobe_get_capabilities` | `pixelprobe_system`（action=capabilities） |
| `pixelprobe_inspect_media` | `pixelprobe_overview`（detail=meta；原 standard 扫描改为 detail=scan） |
| `pixelprobe_get_frame` | `pixelprobe_look`（view=frame，at="f=120 rect(...)"） |
| `pixelprobe_read_pixels` | `pixelprobe_measure`（what=pixel，at="f=12 point(x,y)"；原生样本 options.sample=native） |
| `pixelprobe_analyze_region` | `pixelprobe_measure`（what=region，at="rect(...)"） |
| `pixelprobe_find_changes` | `pixelprobe_find`（kind=changes），或用 query 搜索语句 |
| `pixelprobe_generate_representation` | `pixelprobe_bundle`（action=generate，requests=[...]） |
| `pixelprobe_evidence` | `pixelprobe_evidence`（参数移入 options；视频自动给出码流结构） |
| `pixelprobe_workflow` | first_change → `pixelprobe_find` kind=first_change；compare_media → `pixelprobe_compare` 带 reference_path；quality_overview → `pixelprobe_overview` detail=quality |
| `pixelprobe_list_artifacts` / `pixelprobe_read_artifact` | `pixelprobe_bundle`（action=list / read） |
| `pixelprobe_submit_job` | 任意工具加 `wait=false` |
| `pixelprobe_get_job` / `pixelprobe_cancel_job` / `pixelprobe_get_job_result` | `pixelprobe_job`（action=get / cancel / result） |
| `pixelprobe_resume_job` | 已删除：有了索引和缓存，重新提交成本已很低 |
| `pixelprobe_maintain_storage` | `pixelprobe_system`（action=storage，dry_run=false 才删除） |

其他行为变化：

- 帧号、时间、坐标统一写在 `at` 参数里；计算参数放在 `options` 里（各能力可用参数见 `pixelprobe_system`）。
- 返回给 AI 的图片带坐标刻度并按 `max_side` 缩放，缩放比例在 `data.images`；精确数值请用 measure，
  不要从预览读取。1.x 的 `get_frame` 返回不缩放原图，2.0 的 `look` 不再如此。
- 显示模式读取透明或调色板图片时，结果附警告并提示改用 `options.sample=native`；1.x 在显示模式下
  额外附带的 `stored_sample` 字段已移除。
- 证据结果中的 EXIF 只返回字段名（`exif_fields`），完整内容在结果包里。
- 任务结果可含多张图片，按序号分别校验 SHA-256；1.x 留下的任务记录与 checkpoint 仍可由
  `pixelprobe_system action=storage` 按期清理。

MCP 客户端配置不变（`pixelprobe-mcp` 或 `pixelprobe --mcp`）；也可以改用插件，见 [插件安装](installation.md#插件)。

## 数据与缓存

- 结果包（Bundle Schema 0.1）不变，1.x 生成的结果包可直接读取和校验。
- 媒体索引是 2.0 新增的；索引格式在 2.0 内已升为 v2，开发期间建立的 v1 索引会被视为未建立，重建即可。
- 引擎执行缓存与 1.x 兼容，不需要清理。

## Python API

`pixelprobe.generate`、`pixelprobe.explain`、`RepresentationRequest` 与结果包读写接口不变。
删除了内部的 `pixelprobe.compat` 模块（1.x 命令行用的请求翻译层，现由 `pixelprobe.request_builders` 与能力登记表
`pixelprobe.capabilities` 取代）；命令行与 MCP 共用的功能可通过 `pixelprobe.capabilities.capability(name).invoke(...)` 调用。
