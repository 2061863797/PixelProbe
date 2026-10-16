# MCP 与 CLI 实测报告（2026-10-17）

测试对象：PixelProbe 2.0（提交 `c122092`），Windows 11。
方法：同一素材、同一参数分别通过 CLI（`--json`）和真实 MCP stdio 客户端调用，比较数据、错误码和资源行为；另用工作进程的真实内存峰值校准 MCP 的预算估算。
说明：本报告只记录问题，**未修改任何源码**；全量 `pytest` **未执行**。

## 一、测试覆盖

| 范围 | 内容 |
|---|---|
| 能力 | 登记表 30 项中，对 28 项做了 CLI/MCP 同参数对比（`cache`、`doctor` 只做了功能检查） |
| 素材 | 32×32 无损视频、变帧率视频、闪烁/噪声/运动视频、64×48 的 PNG/调色板/Alpha/带 EXIF 的 JPEG、1086×1448 JPEG 与 PNG、2000×1500 与 3000×2000 JPEG、720p/1080p/4K H.264 短视频、空文件、损坏文件 |
| 场景 | 索引（建/看/删/清理）、后台任务（排队、取消、领取、非法 ID）、结果包（校验、列表、读取、篡改）、`generate`、分页与参数上限、错误路径 |
| 未覆盖 | 全量 pytest；真实相机照片与视频；变帧率视频的分析类能力；动图、多页图、CMYK；C2PA 有凭证的情况；4K 在 CLI 上只测了部分能力 |

## 二、问题清单

### 高严重度：MCP 资源预算

**H1. 1080p 及以上视频的基础操作被 MCP 中途杀掉，CLI 正常**
- 现象：`frame`、`pixel`、`changes`、`scan`、`timeline`、`track`、`xt` 在 1080p 视频上报 `MCP_RESOURCE_LIMIT：工作进程实际内存超过预算`。720p 正常，所以小视频测不出来。
- 数据（工作进程真实 RSS 峰值 vs MCP 计划值）：

  | 操作 | 媒体 | 计划 | 真实 |
  |---|---|---|---|
  | frame | 1080p | 191 MiB | 202 MiB |
  | frame | 4K | 381 MiB | 505 MiB |
  | changes | 1080p | 191 MiB | 224 MiB |
  | scan | 1080p | 191 MiB | 237 MiB |
  | xt | 1080p | 192 MiB | 271 MiB |

- 原因：`media_plan` 对视频解码按每像素 32 字节估，实测约 50～56 字节/像素。位置：`src/pixelprobe_mcp/resource_plan.py` 的 `media_plan`。
- 复现：`scratchpad\qa\calib.py`（见第四节）。

**H2. 上限漏算 128MiB 运行时余量，导致多个能力被误拒**
- 现象：与已修复的 evidence 是同一类错误。工作进程的计划内存 = 128MiB 运行时余量 + 分析内存，但默认上限只有 256MiB，分析内存实际只剩一半，而 CLI 的分析预算是完整的 256MiB。
- 涉及的默认上限：`timeline`、`xt`、`yt`、`reduce`、`spectrum`、`spectrum2d`、`flow`、`generate`（`src/pixelprobe_mcp/jobs.py` 的 `CAPABILITY_DEFAULT_RESOURCES`，均为 256MiB）。实测被拒的是 `flow`、`reduce`、`spectrum2d`；其余按同一机制推断，未逐项验证。
- 另外，引擎类能力的估算严重偏高，计划被直接拒绝：

  | 操作 | 媒体 | 计划 | 真实 | 结果 |
  |---|---|---|---|---|
  | flow | 1080p | 1963 MiB | 271 MiB | MCP 拒绝，CLI 可跑 |
  | reduce | 720p | 1105 MiB | 374 MiB | MCP 拒绝，CLI 可跑 |
  | spectrum2d | 1.5MP 图 | 1520 MiB | 374 MiB | MCP 拒绝，CLI 可跑 |

- 位置：`resource_plan.py` 的 `media_plan` 中 `generate_representation` 分支（按全部帧数 × 每帧 32 字节估算，未考虑流式处理）。

**H3. 大任务之后紧跟的小任务被误杀**
- 现象：工作进程复用后，上一个任务残留的内存被算到下一个任务头上。
- 复现：先跑 3MP 图的 `evidence`，紧接着对 32×32 小视频跑 `timeline`，小任务报"实际内存超过预算"，重试后又正常。批量测试时 32×32 视频的 `first_change`、`timeline` 也各被误杀一次；单独在全新服务里重跑则正常。
- 原因推测：`run_operation_sync` 用工作进程总 RSS 与本任务计划值比较（`worker.py`），而池复用条件只要求 RSS ≤ 总预算的一半（默认 256MiB）。未做进一步验证。
- 复现：`scratchpad\qa\repro.py` 的 B 段。

### 中严重度

**M1. 输出名已存在时，MCP 报 `PATH_NOT_ALLOWED`**
- 同一 `output_name` 第二次调用，MCP 报"请检查 PIXELPROBE_MCP_ROOTS"，会误导用户去查白名单；CLI 报 `BUNDLE_TARGET_EXISTS`。
- 复现：`repro.py` 的 A 段；影响 `evidence`、`generate`、后台任务提交。

**M2. 默认索引目录在本机无法写入**
- 现象：`index build` 在默认目录 `%LOCALAPPDATA%\PixelProbe\index` 失败，报 `WinError 17`（目录改名失败）。CLI 与 MCP 表现一致；MCP 只返回泛化的 `MCP_OPERATION_FAILED`。设置 `PIXELPROBE_INDEX_DIR` 后正常。
- 原因：本机 `AppData` 目录带"加密"属性（EFS），目录 `os.replace` 失败。属于环境问题，但索引写入（`src/pixelprobe/index/store.py` 的 `save`）没有备用方案，错误信息也无法让用户自助排查。
- 影响：MCP 没有 `index_dir` 选项，用户只能通过环境变量绕过。

**M3. MCP 的 evidence 不返回 EXIF 的值，只返回字段名**
- `metadata.exif_fields: ["Make","Model"]`，CLI 返回完整 `exif`。可能是出于隐私的有意设计，**需要确认**；若是有意设计，取证场景下 AI 拿不到相机型号、拍摄时间。

**M4. CLI 的 `info` 对图片缺少通道语义**
- MCP 返回 `image_analysis`、`image_samples`、`flags`（例如调色板图的 `INDEXED_COLOR_IMAGE`），CLI 的 JSON 与表格输出都没有。调色板图在 CLI 里只显示"颜色模式 P"。与 MCP 引导语第 10 条（P 模式须结合 palette 解读）不一致。

### 低严重度

| 编号 | 问题 |
|---|---|
| L1 | 文件不存在、空文件、损坏文件：CLI 报 `RUNTIME_ERROR` 并带 Pillow 原始英文与完整路径，MCP 只报 `MCP_OPERATION_FAILED`，两侧都缺少明确的"文件损坏/格式无法识别"错误码 |
| L2 | 读取不存在的 artifact，MCP 报泛化的 `MCP_OPERATION_FAILED` |
| L3 | 领取已取消或失败的任务，提示"任务尚未成功完成"，不准确 |
| L4 | `--json` / `--no-progress` 各命令支持不一致：`cache` 没有 `--json`；`index` 各子命令、`generate` 没有 `--no-progress` |
| L5 | CLI 有而 MCP 没有的选项：`--no-index` / `--index-dir`（`changes`、`find`、`scan`）、`frame --max-width/--max-height`、`timeline --orientation/--scale`、`xt/yt --scale-t/--scale-x/--scale-y`、`doctor --allow-root/--artifact-root`。输出文件路径类选项缺失应为有意设计 |
| L6 | 命名不一致：`find --settle` 对 `settle_seconds`；`sheet --no-annotate` 对 `annotate`；`index prune --all` 对 `all_entries`；`evidence --c2pa-trust-anchors` 对 `c2pa_trust_anchors_pem` |
| L7 | 返回字段差异：`frame` 的 `output_width/output_height`、`timeline` 的 `orientation/scale/raw_*`、`xt/yt` 的 `width/height/scale_*`、`flow/track` 的输出路径字段只在 CLI 里出现 |
| L8 | `info` 对视频，MCP 多返回 `index`（索引状态）字段，CLI 没有 |

## 三、确认没问题的部分

- **数值一致**（CLI 与 MCP 逐项比较，差异为 0）：`pixel`（含原生通道、调色板、Alpha、`pid`）、`compare`、`sheet`、`reduce`（小视频）、`spectrum`、`spectrum2d`（小图）、`streams`、`quality_overview`、`compare_media`。
- **像素真值**：32×32 测试视频第 3 帧 `point(3,8)` 为 (255,0,0)，与素材定义一致。
- **错误语义一致**：地址写错、越界帧号和坐标、矩形为 0 或越界、未知引用、在图片上调用视频能力、`compare` 只给一帧、未知查询条件，两侧错误码和提示基本一致。
- **后台任务**：队列满时返回 `MCP_QUEUE_FULL`；取消可重复调用；取消后任务终态为 `cancelled`，工作进程回收，新任务正常；不存在或含路径符号的任务 ID 被拒绝。
- **结果包**：篡改一个文件后，`validate` 在 CLI 与 MCP 两侧都报 `ARTIFACT_CHECKSUM_MISMATCH`；`metadata_only` 模式不报错（符合文档）。
- **分页与上限**：`limit`、`offset` 生效，`max_side` 越界被拒。
- **本轮三次提交**：`evidence` 在 1086×1448、2000×1500 上 CLI 与 MCP 均通过；除 EXIF 的表示方式不同（见 M3）外，数据一致。3000×2000 在 CLI 与 MCP 上均因预算被拒，两侧一致。

## 四、复现材料（临时目录，未纳入仓库）

测试时使用的临时复现脚本，未纳入仓库：

| 文件 | 用途 |
|---|---|
| `harness.py` | 同参数跑 CLI 与真实 MCP stdio，逐项比较数据（`python harness.py <qa目录> [用例名过滤]`） |
| `calib.py` | 放宽预算运行各能力，输出"默认上限 / 计划 / 真实峰值" |
| `scenarios.py` | 系统、索引、后台任务、结果包、`generate` 场景 |
| `repro.py`、`cancel.py` | 输出名冲突、大任务后误杀、取消与排队的最小复现 |
| `results.json`、`run_all.txt` | 批量对比的原始输出 |

注：这些脚本放在临时目录里，会随会话清理；如需长期保留，需要单独决定是否纳入仓库。

## 五、建议的修复顺序（未执行）

1. **预算问题（H1～H3）**：视频解码系数按实测校准；`CAPABILITY_DEFAULT_RESOURCES` 中各能力上限统一加上运行时余量；引擎类估算改为流式模型；工作进程复用时按"本任务内存增量"判断，或每个大任务后回收。修完用 `calib.py` 复测，并补回归测试。
2. **错误码（M1、L1～L3）**：输出已存在使用专用错误码；损坏文件增加明确错误码。
3. **对等性（M3、M4、L4～L8）**：先确认 EXIF 值是否有意隐藏，再决定 `info` 的图片信息、选项命名和 `--json` 的统一。
4. **环境健壮性（M2）**：索引写入增加备用方案，错误信息给出可操作的建议。

## 六、修复记录（2026-10-17）

| 编号 | 处理 | 位置 |
|---|---|---|
| H3 | 只把本任务期间新创的 `peak_wset` 峰值计入；常驻内存超过运行时余量（128 MiB）的工作进程不再复用 | `worker.py` |
| H1 | 视频解码系数 32 → 72 字节/像素；引擎视频表示另计 8 帧帧存储驻留页；组合流程系数 96 → 120 | `resource_plan.py` |
| H2 | 引擎类估算按真实数据流建模（切片只算几何采样、时间聚合按流式工作集、光流/二维频谱只算一对帧/一张图）；默认预留统一含运行时余量：媒体 384 MiB、引擎类 512 MiB（报回计划后按计划收缩） | `resource_plan.py`、`jobs.py` |
| M1 | 输出名已存在改报 `BUNDLE_TARGET_EXISTS` | `config.py` |
| L1 | 不存在报 `FILE_NOT_FOUND`（先判白名单，不借此探测外部文件）；空文件、无法识别报 `UNSUPPORTED_MEDIA`；消息不回显文件名 | `config.py`、`media_reader.py` |
| L2 | 未知 artifact 报 `ARTIFACT_NOT_FOUND`（仍是 `KeyError` 子类，兼容旧调用方） | `artifacts/errors.py`、`bundle.py` |
| L3 | 错误码不变（`MCP_JOB_NOT_READY`），按状态说明：排队/运行中可稍后领取；失败/取消/超时/中断"不会产生结果" | `jobs.py` |
| M2 | 实测该目录连普通文件同目录改名都失败（WinError 17）；改名被拒时退回逐文件复制、manifest 最后写；其余写入失败报 `OUTPUT_WRITE_FAILED` 并提示 `PIXELPROBE_INDEX_DIR` | `index/store.py` |

复测（全新工作进程逐项运行，默认 512 MiB 总预算）：1080p 及以下的 `frame/pixel/changes/scan/timeline/track/flow/xt/yt/reduce/spectrum2d/quality_overview/first_change/evidence/sheet/region` 全部通过，计划值均不低于真实峰值；4K 的 `frame`、`xt` 计划 698/888 MiB，在预检阶段明确拒绝（需调高 `PIXELPROBE_MCP_TOTAL_MEMORY_BYTES`），不再中途被杀。`repro.py` 的 A、B 段均已正常。

### 第二轮（同日）

| 编号 | 处理 | 位置 |
|---|---|---|
| 新 | `scan`/`changes`/建索引改用顺序扫描系数 88 字节/像素（实测 2K/4K 约 70，单帧系数 72 不够） | `resource_plan.py` |
| 新 | 只被其他计算节点消费的中间结果（如 `reduce`/`spectrum` 的整段帧）落到临时文件；直接作为输出的结果仍按输出格式决定，内存输出超限照常报错。1080p `reduce`/`spectrum` 两侧恢复可用 | `engine/executor.py` |
| 4K | 新增 `PIXELPROBE_MCP_TOTAL_MEMORY_MB`（留空为 512）；插件新增配置项 `total_memory_mb`；本仓库 `.mcp.json` 设为 4096 | `config.py`、插件清单 |
| M3 | 两侧都返回 EXIF 值（新增读取 Exif 子目录：拍摄时间、镜头等）；二进制或超长字段只列名（`exif_omitted_fields`）；GPS 默认只报 `gps_present`，`include_gps=true`（CLI `--include-gps`）才返回；结果包内报告保留完整值 | `evidence.py`、`capabilities/bundles.py` |
| M4、L8 | `info` 的图片通道语义/调色板/透明度/提示标志和视频索引状态移入共用能力，CLI 新增 `--no-index/--index-dir` | `capabilities/media.py`、`image_report.py`、`cli/info_cmd.py` |
| L4 | 复核：`cache` 各子命令本有 `--json`，`index build` 本有 `--no-progress`；为便于脚本统一追加，`index show/remove/prune` 与 `generate` 接受 `--no-progress`（这些命令本无进度输出） | `cli/` |
| L5 | MCP 的 `pixelprobe_find`、`pixelprobe_overview` 新增 `no_index`；索引目录、显示缩放类选项按设计不加 | `models.py`、`server.py` |
| L7 | 逐项比对：MCP 数据字段已是 CLI 的子集，CLI 独有字段全部描述 CLI 写出的 PNG/CSV 文件（路径、像素尺寸、放大倍数），MCP 对应信息在 `data.images`；不新增，改为对等测试锁定"除这些文件字段外两侧完全一致" | `tests/test_capability_parity.py` |
| L6 | 按决定暂不改名 | — |
| 新 | 组合流程系数 120 → 132（4K 实测 1084 MiB 超出原计划 1077）；组合流程默认预留改为 512 MiB | `resource_plan.py`、`jobs.py` |

复测（每项全新工作进程）：

- 默认 512 MB：1080p 的 frame/changes/scan/quality_overview 全部通过。
- 4096 MB：2K（2560×1440）的 frame、pixel、changes、scan、timeline、xt、flow、quality_overview、建索引全部通过；4K 除 `flow` 外全部通过，计划值均不低于真实峰值。
- **4K `flow` 仍不可用（CLI 与 MCP 一致）**：引擎对单个算子默认限 256 MiB，Farneback 在 4K 需约 506 MiB，与 MCP 总预算无关，未改动。

### 第三轮：内存控制改为"按机器给上限 + 撞线后重试"

参考 Dask（`memory_limit="auto"`、按比例阈值）、Ray（整机内存监视、被停任务重新调度并保证至少一个能跑）、Nextflow/Snakemake（内存类失败按次加大内存重试）：

| 改动 | 位置 |
|---|---|
| 新增 `machine_memory_budget()`：物理内存一半与空闲内存 70% 取小，至少 256 MiB，`PIXELPROBE_MAX_MEMORY_MB` 可覆盖；CLI 证据/组合流程/引擎默认上限与 MCP 默认总预算都用它（本机测得约 8 GB） | `utils/resource_budget.py`、`config.py`、`evidence.py`、`workflows.py`、`api/generate.py` |
| 预估只用于排队和事先拒绝；执行期停止线改为任务额度（总预算 ÷ 进程数），内存预留不再随估算收缩；引擎单步上限跟随任务额度（显式 `resources` 照常遵守，计划 ID 不变） | `worker.py`、`jobs.py`、`service.py` |
| 整机空闲内存低于物理内存 5%（至少 256 MiB）时不启动新任务，运行中的任务停止 | `worker.py`、`jobs.py` |
| 内存类失败（超额度、分配失败、整机过低、引擎单步上限）等其他任务结束后用整份总预算单独重跑一次，任务状态 `memory_retry` 记录首次原因与两次额度；非内存类失败不重试 | `jobs.py` |
| 外溢临时文件计入临时空间计划，修复 1080p `spectrum` 临时空间不足 | `resource_plan.py` |

真实 MCP 复测（不设任何内存变量）：4K 的单帧、变化检测、扫描、**光流**、时间线、质量总览、建索引，2K 扫描，1080p `spectrum`/`reduce` 全部成功。重试实测：总预算 900 MB 时 2K 光流首次按 512 MB 被拒，独占 900 MB 重跑成功（峰值 423 MB）。

全量测试：597 通过，0 失败，0 跳过。数值基线 7 处不一致，均为新增字段（`info` 的图片分析与索引状态、`evidence` 的 `gps_present`）和环境中 c2pa 0.37.10 → 0.37.12，无数值变化；基线未更新，需单独提交。
