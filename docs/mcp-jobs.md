# MCP 工具与后台任务（2.0）

## 10 个工具

MCP 只有 10 个入口，用参数选择具体能力；每项能力与命令行共用同一个执行函数，
数值结果相同（由 `tests/test_capability_parity.py` 把关）。位置统一写在 `at` 参数里，
写法与命令行 `--at` 相同，见 [地址写法](address.md)。

| 工具 | 选择参数 | 对应能力（命令行） |
|---|---|---|
| `pixelprobe_overview` | `detail=meta/scan/quality` | `info` / `scan` / `workflow --mode quality_overview` |
| `pixelprobe_look` | `view=frame/sheet/xt/yt/reduce` | `frame` / `sheet` / `xt` / `yt` / `reduce` |
| `pixelprobe_measure` | `what=pixel/region/timeline/spectrum/spectrum2d/flow` | 同名命令 |
| `pixelprobe_find` | `kind=changes/first_change` | `changes` / `workflow --mode first_change` |
| `pixelprobe_compare` | 不给 / 给出 `reference_path` | `compare` / `workflow --mode compare_media` |
| `pixelprobe_evidence` | — | `evidence` |
| `pixelprobe_bundle` | `action=generate/validate/list/read` | `generate` / `validate`；list/read 读取结果包 |
| `pixelprobe_index` | `action=build/show/remove/prune` | `index build/show/remove/prune` |
| `pixelprobe_job` | `action=get/cancel/result` | 后台任务查询、取消、领取 |
| `pixelprobe_system` | `action=capabilities/doctor/cache/storage` | `doctor` / `cache`；能力清单与任务存储维护 |

`pixelprobe_system action=capabilities` 列出全部能力、各自接受的 `at` 写法、计算参数
（`options`）和对应命令行。

### 带刻度的图

`look`、`compare` 默认返回图片；`measure`、`find` 传 `images=true` 时附带对应图片。
图片四周标有原始坐标刻度（切片图的时间轴标帧号），`grid=true` 时叠加 8×6 网格
（A1…H6，与地址里的 `cell(...)` 同一套定义）。图片按 `max_side` 缩放，
`data.images[]` 记录缩放比例和坐标换算，**精确数值请用 measure，不要从预览读取**。

### 结果编号

`find`、`overview(scan)`、`overview(meta)`（已建索引时）返回 `refs`，例如
`event1 → f=14..16`、`anomaly1 → f=15`、`seg3 → f=120..245`。服务器按媒体记住这些编号，
下一次调用可直接写 `at="@event1"`；也可在 `refs` 参数里显式传入名称到地址的映射。

## 后台任务

所有媒体操作都经过同一个有界 FIFO 队列、资源预算、超时与取消。默认 `wait=true`
直接返回结果；长视频或耗时操作传 `wait=false`，立即得到 `job_id`：

1. `pixelprobe_job action=get job_id=...` 查询状态与 `resource_plan`、`resource_usage`；
2. `pixelprobe_job action=cancel` 取消，排队中的任务立即取消，运行中的任务协作退出、超时后强制回收；
3. 状态为 `succeeded` 后，`pixelprobe_job action=result` 领取结果（含全部图片）。

任务状态区分排队、运行、成功、失败、取消、超时和中断；只有原子提交且每张图片的
SHA-256 都核对通过的结果才能领取。服务重启只把未完成的任务标记为 `interrupted`，
不会自动重新执行。

2.0 起不再提供 checkpoint 恢复（`resume_job`）：有了媒体索引和执行缓存，重新提交的成本已经很低。
CLI 和 Python API 的 `generate --checkpoint` 不受影响。

## 限制与维护

任务记录默认保留 7 天；正式 Bundle 不随任务记录删除。调用
`pixelprobe_system action=storage` 预览存储占用、过期任务数量与可回收字节；
显式传入 `dry_run=false` 才执行清理。过期依据任务完成时间计算，
不仅凭文件修改时间删除记录。活动任务或其他存活服务实例存在时拒绝清理。
目录或记录异常时保留相关引用并报告 skipped；未知文件和无法确定归属的目录不删除。

1.x 留下的任务记录与 `.checkpoints/` 目录仍可由该接口按期清理：
仍有保留任务引用的 checkpoint 不会删除；有明确程序归属标记、无任务引用且已过期的
孤立 checkpoint 会被清理；无法确认归属的历史目录保留。
独立 CLI/Python 缓存使用自身的引用保护和 cache inspect/prune，见 [缓存维护](cache-maintenance.md)。

资源预留、配置方式和采样监控边界见 [资源控制](resource-control.md)。
