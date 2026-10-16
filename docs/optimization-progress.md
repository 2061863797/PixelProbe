# 优化实施检查点

## 当前状态：2026-10-16 2.0 重构阶段 6：Claude Code 与 Codex 插件

本节优先于下方历史检查点。

- 先查官方文档确认格式（code.claude.com 插件清单与市场参考；developers.openai.com Codex 插件构建页、
  openai/plugins 官方示例、agent-plugins.org MCP schema）。
- 新增 `plugins/pixelprobe/`：Claude Code 清单 `.claude-plugin/plugin.json` + `.mcp.json`（`userConfig.media_roots`
  传入允许目录）；Codex 便携清单 `plugin.json` + `mcp.json`，以及兼容层 `.codex-plugin/plugin.json` + `codex-mcp.json`；
  5 个 Skill（pixelprobe-core、ui-recording、long-video、quality、forensics）。仓库根目录新增
  `.claude-plugin/marketplace.json` 与 `.agents/plugins/marketplace.json`。启动命令固定 `v2.0.0` 标签。
- 实测发现：Codex 0.162.1 读的是兼容层（`.codex-plugin` + 旧格式 MCP 文件），不读根目录便携 `mcp.json`；
  且插件 MCP 服务与用户手动配置的同名服务冲突时会被整个遮住。因此 Codex 侧服务名改为 `pixelprobe-plugin`，
  两种格式都提供。
- 验证：`claude plugin validate` 插件与市场均通过；`claude plugin details` 识别 5 Skill + 1 MCP（常驻约 460 token）；
  Codex 本地市场安装后会话中 5 个 Skill 与 10 个工具全部出现。测试后卸载并确认 `config.toml` 与备份一致。
  新增 `tests/test_plugin_manifest.py`（版本、启动命令、Skill 名与工具名一致性）。全量 **549 passed、0 skipped**。
- 未执行：`v2.0.0` 标签的 uvx 冷启动（发布时创建标签）；Claude Code 命令行会话内实际调用（本机 CLI 登录过期，
  未代为登录）；Codex 中工具调用被测试时设置的"禁止审批"策略拦下，只验证到工具加载；macOS/Linux。
  按项目规则，阶段 6 标记为"配置完成"，不是"验收通过"。

## 历史状态：2026-10-15 2.0 重构阶段 5（一）：find 搜索语言

阶段 5 按小项分别提交。

- 新增 `pixelprobe.find`（解析 + 求值）与能力 `find`：CLI `pixelprobe find <视频> "<语句>"`，
  MCP `pixelprobe_find query=...`。条件：black/white/flat、flash、cut、duplicate、freeze、gap、
  similar、整帧与区域指标比较、区域变化（first）。说明见 `docs/find.md`。
- 每帧体检值抽成 `index.builder.frame_metrics`，索引建立与无索引解码共用；整帧条件有索引时查表，
  否则解码现算，两条路径逐值相同（测试覆盖三种范围）。
- 设计取舍：切换/闪帧采用固定跳变阈值（参考 PySceneDetect），不用 changes 的自动阈值——
  短片中多次大跳变会抬高自动阈值而漏检（测试素材中 120/255 的跳变即被漏掉）；
  差分哈希对纯色画面恒为 0，闪帧判定同时比较前后两帧的通道均值；freeze 用整帧平均会稀释
  小面积变化，已写入定义并指向区域条件。
- 验证：全量 **530 passed、0 skipped**；数值基线 38/38；新增 `tests/test_find.py`（10 项）、
  对等测试新增 find 的 CLI/MCP 逐项一致、MCP 端到端（@match1 引用、错误码 INVALID_QUERY）。
- 未执行：真实录屏与长视频上的阈值校准（目前只用合成素材验证定义）。

### 阶段 5（二）：动画时长、码流结构、块效应

- find 新增 `animation in <区域>`：区域从开始变化到停稳的时长（中途静止不足 settle_seconds 合并），
  给出毫秒值、帧间隔精度和是否停稳。录屏掉帧/卡顿由 gap、duplicate、freeze 覆盖，截图对比由 compare 覆盖。
- 新增能力 `streams`（CLI `pixelprobe streams`，MCP `pixelprobe_evidence kind=streams`，视频扩展名自动选择）：
  容器与流参数、关键帧间隔、B 帧推断、时间戳回退/重复/空隙、包大小与每秒字节、可选 I/P/B 统计，
  线索附证据与局限，不做真伪结论。不分析 QP（PyAV 不提供）。说明见 `docs/streams.md`。
- L1 新增 `blockiness`（8 像素块边界相对强度），find 可用 `frame.blockiness`；索引格式升为 v2，
  旧索引视为未建立。整数亮度 + 隔行抽样后 1080p 每帧约 8 毫秒（初版 22 毫秒）。
- 计划中"镜头切换写入 L2"改为由 `find "cut"` 查 L1 完成（有索引时即时返回），不另存一份。
- 验证：全量 **538 passed、0 skipped**；数值基线 38/38。

### 阶段 5（三）：新切法（阶段 5 完成）

- xt / yt 新增 `source=diff`（与上一展示帧的逐通道绝对差，与先差分再切片逐值相同）和
  `time_axis=adaptive`（权重一半按变化量、一半均匀，按累计权重取帧，用满 rows 行；有索引时查 L1）。
  引擎的 diff 特征只接受整帧序列，因此差分在能力层由切片结果计算，数学上等价。
- 新增能力 `track`（CLI `pixelprobe track`，MCP `pixelprobe_measure what=track`）：逐帧亮度块匹配，
  整数像素精度，超阈值判定跟丢并停止；输出轨迹、位移、速度、跟随切片与首帧轨迹图。说明见 `docs/slices.md`。
- 真实 stdio 进程验证中发现：单帧白闪使自适应取帧只剩 3 行（要求 12 行），已改为混合权重 + 补足间隔，
  并补回归测试。
- 验证：全量 **545 passed、0 skipped**；数值基线 38/38；真实 stdio 进程调用 find、@match1、
  差分 + 自适应切片、track（含图片）、evidence 自动选 streams 全部成功。
- 未执行：真实录屏、长视频上的阈值与跟踪效果校准。

## 历史状态：2026-10-15 2.0 重构阶段 4（能力登记表、MCP 精简为 10 个工具）

分两次提交：能力登记表与命令行改造（a6383d6），MCP 改造（404f9af）。

- 新增 `pixelprobe.capabilities`：27 项能力各登记一次（名称、地址规则、参数模型、执行函数、
  命令行入口、MCP 工具与选择值）。命令行命令改为薄包装；MCP 的 `capability` 操作在工作进程中
  调用同一函数，只额外负责路径白名单、资源边界、受控输出位置、图片编码和分页。
- MCP 由 17 个工具换成 10 个（overview / look / measure / find / compare / evidence / bundle /
  index / job / system），不保留旧名。删除 `resume_job` 与 MCP checkpoint；1.x 留下的任务记录
  与 checkpoint 仍可由 `system action=storage` 按期清理。对照表见 `docs/mcp-jobs.md`。
- 新增 `pixelprobe.output.annotate`：图片四周画原始坐标刻度（切片时间轴标帧号）、可选 8×6
  网格（与地址 `cell(...)` 同一定义），按 `max_side` 与载荷上限缩放，比例写入 `data.images`。
- 结果编号：`find`、`overview(scan)`、已建索引时的 `overview(meta)` 返回 `refs`，服务器按媒体
  记住，下一次可直接写 `@event1`。
- 改造中发现并修复的问题：
  - 改写时漏掉的三项保护已补回：Prompt 中把用户输入包成"不可信 JSON"（防提示注入）、
    `generate` 源解码帧上限 1800、证据结果只返回 EXIF 字段名（防止响应超限）；
  - 资源计划在图片上把组合流程当成单帧读取、预留 0 字节临时空间，导致图片的质量概览失败；
  - MCP 自身的参数错误原先统一显示为"操作失败"，现在返回具体原因（不回显路径和能力名）；
  - 透明或调色板图片在显示 RGB8 下读取时给出警告，提示改用 `options.sample=native`。
- 新增对等测试 `tests/test_capability_parity.py`：每项能力都有可用的命令行命令和 MCP 入口，
  逐个按登记的选择值调用 MCP 工具确认落到同名能力；12 个抽样（图片/视频的 info、pixel、region、
  changes、scan、timeline、spectrum、spectrum2d、flow）命令行与 MCP 数值逐项相同。
- 重写 MCP 相关测试（server、worker、jobs、maintenance、资源、组合流程、发行、证据、C2PA、
  索引），删除 `test_mcp_resume.py`（7 项）。评估题集改用新工具说法并新增 2 题（@编号、网格）。
- 验证：全量 **507 passed、0 failed、0 skipped**；数值基线 38/38 一致；真实 stdio 子进程
  逐个调用 10 个工具全部成功（含 @event1 引用、cell 网格、索引、wait=false 后台任务）。
- 未执行：在 Claude Code 中以 MCP 客户端实际连接本次代码（需要重启客户端加载新版本）；
  Linux/macOS 运行。

## 历史状态：2026-10-14 2.0 重构阶段 3（媒体索引、会话与常驻工作进程）

分两次提交：索引与命令行接入（543ff5e），会话与常驻进程（b3857d5）。

- 新增 `pixelprobe.index`：一次顺序解码生成 L0 元数据、L1 每帧体检表、L2 变化/平稳分段、
  L3 缩略立方体（长边 64、默认总量 ≤128 MB，超出按 thumb_stride 隔帧保留）。按快速指纹
  分目录，先写临时目录再原子提交；同指纹已有有效索引时保留旧份，损坏时在确认无活动引用后替换；
  删除与清理复用缓存的 gate/reference/require_idle。说明见 `docs/media-index.md`。
- 新增 `pixelprobe index build/show/remove/prune`，索引目录由 `--index-dir` 或
  `PIXELPROBE_INDEX_DIR` 指定。`changes` 整帧逐帧、`scan` 采样间隔为 1 时直接查表，
  与解码逐值相同（含 scan 网格图像素）；条件不满足自动退回解码，输出新增 `computed_from`。
  计划中"其他命令自动使用索引"收窄为"仅在可保证逐值相同时使用"：逐帧差异无法推出隔帧差异，
  长视频 scan 自动隔帧采样时仍解码。
- 新增 `pixelprobe.session`：进程内按快速指纹缓存已加载索引（最多 4 个媒体），并为阶段 4 的
  `@编号` 预留登记表；MCP 的 find_changes 与 standard 概览已接入。
- MCP 工作进程改为常驻进程池：正常结束回空闲池并优先复用同一媒体的进程；超时、取消、
  实际内存超限、监控失败、崩溃或通信异常一律回收不复用；累计 200 个任务、任务后常驻内存超过
  总预算一半、空闲超 300 秒或空闲数达上限时退出。每任务的内存上限、临时目录、超时与协作取消
  保持不变。实测（测试视频，连续 10 次读取）：总耗时 5.37 秒 → 0.81 秒，首次之后单次
  约 0.514 秒 → 0.016 秒。
- 测试：新增索引 14 项（逐值一致、失效、容量上限、并发提交、损坏替换、引用保护、CLI、
  会话、真实工作进程）与进程池 2 项（复用同一进程、取消后不复用）；两项"请求后不得残留子进程"
  的测试改为"超时/超限进程必须回收，关闭进程池后不残留"。测试会话与数值基线隔离到临时索引目录。
- 验证：全量 **497 passed、0 skipped，130 秒**；数值基线 38/38 一致。
- 过程说明：阶段 3（上）提交前的全量运行有 2 项失败，原因是测试运行期间修改了
  `worker.py`、被测试新启动的子进程读到改了一半的文件；在该提交的独立副本上重跑相关 52 项
  全部通过，之后的全量运行期间不再改动代码。
- 未执行：Linux/macOS 运行；长时间（数小时）常驻进程的内存增长观测。

## 历史状态：2026-10-14 2.0 重构阶段 2（统一地址写法）

- 新增 `src/pixelprobe/address/`（模型、手写解析器、换算，共 733 行，无新依赖）。
  语法：时间 `f=`/`t=`（单帧、闭区间、开放端、两帧列表、all；时间支持 s/ms/时间码）、
  `step=`、形状 `point/pid/rect/grid(... step=n)/row/col/cell/line/path`、引用 `@名称`。
  `str(address)` 给出规范写法并可再次解析。`cell` 默认 8×6，边界按 floor 划分、无缝拼满。
- 新错误码 `INVALID_ADDRESS`（`InvalidRangeError` 子类，退出码 2）；错误信息给出出错位置、
  原因和写法示例，命令不接受的写法会列出该命令可用的写法。
- 15 个命令改用 `--at` 与 `--ref-file`，删除旧的时间/位置参数（按"直接切换"决定）。
  语义调整：`step=` 只表示时间采样，网格间距写在 `grid(... step=n)` 内；`pixel` 的
  point/pid 按书写顺序输出；`workflow` 只接受帧号范围；`scan` 只接受 `step=`。
- 数值基线用例全部改用 `--at` 写法，期望值保持阶段 0 采集的结果，38/38 一致，
  证明新写法与旧参数计算结果完全相同。
- 新增 `docs/address.md`（含各命令接受的写法和 1.x 迁移对照表）；README 中英文、
  `docs/workflows.md`、`docs/positioning.md` 示例同步更新。`AGENTS.md` 的 Antigravity
  条款改为"默认父代理直接完成，用户明确要求时才委派"，与该 Skill 的触发条件一致。
- 验证：全量 **481 passed、0 skipped，116 秒**（新增 48 项地址测试、2 项命令行测试）。
- 未改动：MCP 工具参数（阶段 4 统一替换）；`@引用` 目前只能由 `--ref-file` 提供，
  结果自动生成引用编号在阶段 3/4 实现。未执行：Linux/macOS 运行。
- 过程说明：`tests/test_frame.py` 的旧参数替换使用了一段带唯一性断言的脚本，
  不符合"用编辑工具修改"的规则；之后的文件均改用编辑工具，改动可在提交 diff 中复核。

## 历史状态：2026-10-14 2.0 重构阶段 1（统一计算核心）

- 实际核查修正了计划中的判断：`compat/` 主要是"CLI 参数 → 统一请求"的构造函数，
  并非多余的来回翻译；真正多余的是 CLI/MCP 从未调用、只被测试使用的直接解码实现
  （`core/frequency.py`、`core/temporal_reduce.py`、`core/spacetime_slice.py`、
  `core/optical_flow.py`、`extract_timelines`，以及 `operators/sampling.py` 中的
  `sample_xt/yt/points_t/path_t/roi_t` 与 `execute_sampling`）。
- 删除上述实现与 `compat/` 包；请求构造函数迁入 `src/pixelprobe/request_builders.py`
  （去掉 `legacy_` 前缀，重复六次的"闭区间→半开区间"合并为 `interval_selection`）。
  Farneback 固定参数与灰度转换迁入 `operators/optical_flow.py`。FFT、光流算法现在只在
  `operators/` 各有一处实现。`pixelprobe` 包由约 18,070 行降到 16,699 行。
- 引擎文件身份改为两级（`utils/fingerprint.py`）：内存/NPY 输出用快速指纹（大小、
  修改时间、文件标识、头尾各 1 MiB），Bundle/Zarr 输出仍用完整 SHA-256 并写入溯源；
  溯源记录只在结果包中生成，快速指纹不会冒充 sha256。1 GiB 文件实测（已在系统缓存）：
  每次查询的身份计算由 1.571 秒降到 0.003 秒。局限已写入模块文档：中间内容被改写而
  大小、修改时间、头尾不变时快速指纹无法察觉。
- 测试迁移：原先"引擎与旧实现逐值一致"的断言改为与合成素材真值比较、与不中断执行
  比较，或改由数值基线锁定；新增 8 个基线用例（diff+destripe+smooth、median 区间、
  光流补偿与累积、块均值网格时间线、采样 X-T、点频谱、区域二维频谱），这些期望值
  在阶段 0 提交的代码上采集。`max_median_bytes` 只保护已删除的 Python 函数，相应测试
  改为验证引擎的内存预算拒绝。`docs/api.md` 改为 request_builders + generate 用法。
- 验证：全量 **431 passed、0 skipped，112 秒**（基线 429，新增快速指纹与 Bundle
  完整哈希两项测试）；`capture_golden.py --check` 38/38 一致；CodeGraph 已同步，
  `np.fft`/Farneback/仿射估计只出现在 `operators/frequency.py` 与
  `operators/optical_flow.py`。
- 未执行：Linux/macOS 运行。

## 历史状态：2026-10-14 2.0 重构阶段 0（定位、规则与基线）

本节优先于下方历史检查点。2.0 的定位、边界和路线图见 `docs/positioning.md`。

- 新增 `docs/positioning.md`：定位为"补齐 AI 看图看视频的短板"，覆盖开发者录屏、
  长视频辅助、质量异常、取证四类场景；CLI 与 MCP 能力对等；2.0 直接切换新接口。
- `AGENTS.md` 的兼容条款改为以 2.0 新接口为准（Bundle 0.1 读取仍兼容），新增能力对等
  与数值基线规则；编辑工具说明改为通用写法。本地蓝图已归档到 `docs/design/archive/`
  （仍不入库）。
- 新增 `scripts/capture_golden.py` 与 `tests/golden/cli_golden.json`：30 个 CLI 用例
  覆盖 info/frame/pixel/region/timeline/xt/yt/changes/reduce/compare/sheet/scan/
  spectrum/spectrum2d/flow/workflow/evidence，记录归一化 JSON 数值与输出文件像素摘要。
  测试视频容器带随机 UID，源文件哈希字段不参与比对。连续两次 `--check` 全部一致。
  基线未纳入 pytest：示意图含字体渲染，跨平台像素可能不同，只在本机重构前后比对。
- 重构前全量基线（`.venv-test`，Python 3.14.3，含 Zarr/OpenCV/MCP/C2PA）：
  **429 passed、0 skipped，106 秒**。系统 Python 未安装 Zarr，会有 4 个 Zarr 用例失败，
  后续统一用 `.venv-test` 运行。
- 未执行：CodeGraph 同步（本阶段未改源码）、Linux/macOS 运行。

## 当前状态：2026-09-20 正式发行改为 Python 包与本地打包脚本

本节优先于下方历史检查点。正式发行不再包含冻结程序；Linux/macOS 的本地
冻结程序实测不属于发布门槛，并继续按用户要求暂缓。下方历史检查点保留当时的
实施记录，其中已经取消的独立版目标不再表示当前待办。

- 正式 GitHub Release 和 CI 已移除 Windows/Linux/macOS PyInstaller 构建、冻结程序
  上传和相关原生 wheel 下载；标签只发布 PixelProbe wheel、sdist 和校验和。
- 新增 scripts/build_local_standalone.py。开发者可用当前环境生成私人单文件程序；
  输出目录拒绝覆盖，先在同级临时目录构建，启动/帮助/doctor 和默认真实 CLI/MCP
  验收全部成功后才原子提交。
- 本地输出包含 README、项目 LICENSE、LOCAL-BUILD.txt 和 build-info.json；后者记录
  local_private_build 范围、平台、Python、实际依赖版本、程序 SHA256，并明确
  public_redistribution_supported=false。脚本不会把本地产物上传或发布。
- README 中英文版、安装、uvx 与发行验收文档均改为新的发行边界；
  正式用户使用固定 wheel 或 uvx，本地单文件不得误称官方发行物。
- 删除只服务于官方独立版的原生 wheel 指纹、再分发约束、许可补充、清单生成器、
  专项测试和原生审查文档；本地脚本实际依赖的 PyInstaller spec 与冻结版入口测试保留。
- Windows 本地脚本已完成一次真实构建：产出 204346072 字节的 `pixelprobe.exe`，
  `--version`、`--help`、`doctor --json` 和冻结版 CLI/MCP 验收通过；测试结果为
  **5 passed、22 deselected，59.07 秒**。`build-info.json` 记录的 SHA256 与独立
  复算结果一致，且无同名前缀的临时目录残留。
- 清理后新建 sdist 与 wheel 成功；sdist 已核对包含本地打包脚本和 PyInstaller
  spec，并排除已删除的原生审计资产。本机验证环境未安装 Twine，因此未执行本轮
  本地 `twine check`；正式工作流仍会安装 Twine 并运行该检查。
- 推送前全量测试发现并改正两个仍要求发布独立程序的过时工作流断言；清理后的
  首轮全量又稳定复现 Zarr 临时读写 store 未关闭导致 Windows 目录重命名返回
  `WinError 5`。写入和校验句柄现均在提交前关闭，公开的 Zarr handle 也提供
  `close()`；相关专项 **6 passed**。
- 删除 5 个已经失去对象的原生发行清单测试后，最终全量回归为
  **428 passed、0 skipped，102.44 秒**。本地构建/正式发布边界专项 **4 passed**；
  旧发行引用搜索、工作流 YAML 解析、脚本语法检查和 `git diff --check` 均通过。
- 发布候选版本已更新为 **1.1.0**，README、安装文档和 uvx 配置统一固定该版本。
  首个 wheel 的真实 uvx Zarr 往返验收暴露 Windows 在关闭 store 后仍可能短暂拒绝
  目录重命名；提交现在仅在 Windows 对 `PermissionError` 做最多 2 秒的有限重试，
  其他平台和其他异常立即返回。对应回归覆盖一次瞬时失败后成功提交。
- 全量复验还发现 `doctor` 的匿名临时文件在 Windows 上可能短暂显示为待删除目录项。
  写入检查现改为显式命名探针，刷盘、关闭后主动删除，并仅对 Windows 短暂占用做
  最多 1 秒重试；诊断完成时不留下输出目录或探针文件。
- 最终源码全量回归为 **429 passed、0 skipped，113.65 秒**。从该源码重建的 final
  wheel 通过离线 uvx 的 `--version`、完整 `doctor --json`、真实 MCP stdio 握手、
  CLI/MCP 分发与 Zarr 往返验收：**5 passed、0 skipped，34.85 秒**；doctor 中 av、
  numpy、Pillow、psutil、MCP、OpenCV、Zarr、C2PA 和目录配置均为可用。
- 最终本地产物位于 `build/release-1.1.0-final-20260920/`。wheel SHA256 为
  `1a45c2daf531fc65f8b4133f0c2e7cc92a52eb5c70cb46636cbb9a2237f1fbde`，sdist SHA256
  为 `145827847b249abfc139d0bd0d58994fcce9c5c364d81fcddc5cd3803853b13c`；包内两项
  Windows 修复与当前源码哈希一致，sdist 共 203 个成员，清单边界复验通过。
- 本机仍未安装 Twine；`twine check` 由标签发布工作流执行。Linux/macOS 真实运行按
  用户要求未执行。当前尚未提交、推送、创建 `v1.1.0` 标签或发布 Release。

## 历史状态：2026-09-20 原生来源链锁定与正式发布阻断

本节优先于下方历史检查点。Linux/macOS 实测继续按用户要求暂缓。

- 新增 packaging/native-sources.json，固定三个目标平台的 PyAV、C2PA、OpenCV
  官方 wheel SHA256，以及 PyAV/pyav-ffmpeg、c2pa-python/c2pa-rs 的精确标签、
  提交、构建文件和原生发布包指纹；Windows FFmpeg 的 14 个源码输入也已锁定。
- 新增 packaging/release-constraints.txt，独立版构建固定 PyAV 18.1.0、
  C2PA Python 0.37.10、OpenCV 5.0.0.93；版本漂移会由清单生成器明确失败。
- Windows 实际核验官方 wheel：PyAV 252、C2PA 12、OpenCV 45 个安装运行文件
  全部符合 RECORD。C2PA 运行 DLL 与官方 wheel、c2pa-rs 0.90.19 发布包哈希一致；
  两个 `.lib` 构建输入明确排除于运行文件核验。
- 清单生成器复制 native-sources.json，记录核验结果并把它纳入 SHA256SUMS。
  最新审计附件为 build/acceptance-windows/native-audit-v4-20260920/，其中全部附件
  哈希已独立复验。受影响测试最终 **5 passed，1.10 秒**，diff --check 通过；
  发布和 CI 工作流均通过本地 YAML 解析，期间发现并修复 `--only-binary=:all:`
  单行命令造成的 YAML 冒号解析错误。
- 发布工作流先下载固定 wheel 并传给清单生成器。标签发布在构建前检查
  redistribution_review_complete；清单的正式发布参数还会再次检查 wheel 与审查状态。
  精确核对发现 pyav-ffmpeg 补丁将 x264/x265 从 FFmpeg GPL 外部库列表重分类为
  version3；该补丁不能替代授权依据。当前仍有 x264/x265、C2PA Rust 静态依赖及
  OpenCV 内嵌组件许可缺口，实际 CLI
  门槛返回非零并包含明确错误。因此公开发布被有意阻止，不能误报为可发布。
- 全量回归 **430 passed、0 skipped，100.92 秒**；新构建的 sdist 已核对包含来源清单、
  发行约束、许可补充和发行脚本，wheel CRC 完整。全量之后只新增了发行脚本的
  wheel 篡改专项回归；该文件最终 **5 passed**，未为这一个测试重复全量。
  无 Python 全新 Windows 环境仍未执行；未上传、推送、发布或更改项目许可证。

## 历史状态：2026-09-20 原生依赖证据与许可状态分离

本节优先于下方历史检查点。Linux/macOS 实测继续暂缓。

- 补跑上次额度限制中断的 diff --check，通过。
- 直接读取本地 PyAV 18.1.0 实际 avcodec/avutil DLL 的许可与编译配置：
  自报 LGPL v3+，同时启用 libx264/libx265；wheel 含对应 DLL。
  OpenCV 还携带另一套 FFmpeg 和静态内嵌组件，不能用 PyAV 的 BSD 许可替代核对。
- 清单生成器增加 native-evidence.json，将原生导出信息绑定到 DLL SHA256。
  独立比对证据和 dependencies.json 的二进制哈希一致。
- 清单 schema_version=2 明确区分 license_files_collected=true 与
  redistribution_review_complete=false，不再使用含混的 complete 字段。
- 受影响专项 **2 passed，0.98 秒**；本轮未修改媒体引擎，未重复全量回归。
  上次全量结果仍为 428 passed，属于此前验证记录。
- 原生审核附件：build/acceptance-windows/native-audit-20260920/。
  对应审查文档已随官方独立版目标取消而移除，本节仅保留当时结论摘要。
- 仍需取得与当前二进制精确对应的上游源码/补丁/组件版本并完成再分发核对，
  以及无 Python 全新 Windows 系统验收。未宣称正式发布验收完成。
  既有 ZIP 未覆盖；未上传、推送、发布或变更项目许可证。

## 历史状态：2026-09-20 Windows 超时回收、回滚及发行附件验收通过

本节优先于下方历史检查点。用户明确暂缓 Linux/macOS 的真实验收，本轮未执行。

- 冻结程序故障注入测试通过（1 passed，26.70 秒）：只挂起本测试创建的
  multiprocessing worker，使其无法协作取消；产品返回 MCP_TIMEOUT 后独立确认
  worker 已不存在，随后像素读取成功。测试兜底清理不能替代产品回收断言。
- 从已有 Git v1.0.2 标签导出并构建历史 wheel，未读取受限 dist wheel 或修改 ACL。
  scripts/verify_uvx_upgrade.py 离线演练 1.0.2 → 当前本地 1.0.3 候选 → 1.0.2；
  三阶段真实 MCP 像素结果正确，原始 wheel 哈希不变，临时配置恢复。
  用户实际客户端配置和远程发行物不在此次演练范围内。
- 新增运行时依赖闭包/许可清单生成器，记录 56 个运行时包和 PyInstaller，
  收集 109 份包许可/通知文件及 CPython 许可，生成原生文件指纹、精确版本列表、
  dependencies.json、THIRD-PARTY-NOTICES.txt、SHA256SUMS.txt。
  C2PA 0.37.10 的 MIT/Apache 原文从官方固定 tag 补齐，来源与哈希随仓库保存；
  换行固定 LF，生成时复验哈希。缺失许可或已有输出目录会明确失败。
- 发布流程已接入附件生成；本地 ZIP 已组装并逐项验证归档内全部 SHA256，
  组装目录内 exe 的 doctor 全部能力通过。
  路径：build/acceptance-windows/release-1.0.3-local-20260920.zip，
  大小 202908580 字节，SHA256：
  05c90289b463f4b311ca6b43c7e1178ae2bceccb814e9eae8a3003bfd2d51874。
- 全量 **428 passed、0 skipped，100.09 秒**；清单脚本最后调整后定向
  **2 passed，0.92 秒**。diff --check 通过。
- 仍未完成：无 Python 的全新 Windows 系统验收（本机无 WindowsSandbox.exe，
  未启用系统功能或安装虚拟机）；原生库内嵌全部传递组件的再分发许可核对。
  当前清单明确覆盖 Python 包、CPython 和打包工具，不声称是完整原生 SBOM。
- Linux/macOS 实测暂缓；未提交、推送、上传或发布，CodeGraph 索引同步未执行。

## 历史状态：2026-09-20 Windows 冻结版与 uvx 真进程验收通过

本节优先于下方历史检查点。

- 修复 CLI/MCP generate 将明确的 Zarr 输出请求强制转换为默认 NPY Bundle 的问题。
  现在保留 output.format="zarr"，复用既有引擎与 BundleWriter；默认行为不变。
- 新增 tests/test_distribution.py，可针对源码、PIXELPROBE_TEST_EXECUTABLE 或
  PIXELPROBE_TEST_COMMAND 指定的 uvx 命令执行同一套公开 CLI/MCP 验收。
- 实际验证：CLI Zarr 写入与逐值回读、目标 CLI 全量 Bundle 校验、MCP Farneback
  光流与 Zarr 矢量读取、C2PA 真实签名/信任锚验证、运行中取消与槽位恢复。
  取消要求 worker 已返回资源计划；取消结果不可领取、无正式 Bundle，随后像素读取正确。
- 源码专项 3 passed；离线缓存 uvx 专项 3 passed（11.59 秒）；
  Windows 冻结程序含基础 stdio 验收 4 passed（28.41 秒）。
  最终全量回归 **425 passed、0 skipped，74.96 秒**，Python 3.12.7。
- 最新独立程序：build/acceptance-windows/dist-20260920/pixelprobe.exe。
  SHA256：7A96E7275A644DEBC7D62C4C54E0F03BA227DC32447BE196977B20DFDF8BA121。
  uvx 验收使用 wheel-20260920 目录中的本地 1.0.3 候选，不代表远程同版本发行物。
- 三平台 CI/发布配置已接入上述冻结测试；Linux/macOS 实际执行仍未完成。
  可复用命令及覆盖边界见 [发行入口验收](distribution-acceptance.md)。
- 待完成：冻结程序强制终止超时分支、跨版本升级回滚、依赖清单与第三方许可、
  无 Python 全新系统及其余平台验收。旧 dist/1.0.0 wheel 读取遇到操作系统权限拒绝，
  未更改 ACL 或覆盖原文件；c2pa-python wheel 未附许可文件，仍需补齐许可来源。
- 上次因额度未执行的 diff --check 本轮已补跑通过；未提交、推送、发布。
  CodeGraph 索引同步未执行。

## 历史状态：2026-09-19 Windows 发行验收推进

本节优先于下方历史检查点。

- 在 build/acceptance-windows/venv 创建不继承全局包的 Python 3.12.7 环境。
  原 .venv-test 会引入全局 Torch/Pandas/SciPy，已停止使用该环境进行发行构建。
- Windows 单文件已实际生成：build/acceptance-windows/dist/pixelprobe.exe，
  204345858 字节，版本 1.0.3。它是当前未发布源码的本地候选，不能替代同版本远程发行物。
  SHA256：09BF64FF7DB28172A5270F504BE517C76A83D881B47E0B74D0A5D2E98E8F6C97。
- 修复 numcodecs 版本元数据未打包导致冻结 Zarr 无法导入；
  冻结 MCP 测试现在强制检查 av/numpy/PIL/psutil/mcp/cv2/zarr/c2pa 全部可用。
- 修复 Windows JSON 模式沿用 GBK 的问题，成功与错误响应统一 UTF-8。
  增加 GBK/cp1252 回归，真实 MCP 输入改为中文文件名。
- 修复后真实冻结 MCP 验收 1 passed：初始化、像素读取、spawn 工作流及 Bundle 生成。
  空工作目录、清除 Python 环境变量、PATH 仅保留 System32 时 doctor 全部能力通过。
  这不等同于已在完全未安装 Python 的机器上验收。
- 本地 wheel 的 uvx Python 3.12 空缓存首次安装通过；修复后的 wheel-final 候选
  在 --offline 下使用已下载依赖成功，并再次离线复用环境成功。
  尚未完成 uvx 实际 MCP、跨版本升级/回滚或远程正式发行物验收。
- 定向回归 39 passed；最终干净 Python 3.12 环境源码全量 **422 passed、0 skipped，
  69.30 秒**。冻结程序与源码测试分别执行，不混算通过数。
- CI/发布固定构建 Python 3.12，Linux 核心测试保留 3.11/3.12/3.13 矩阵；
  发布前测试补齐 evidence extra。Linux/macOS 执行及 CI 运行均未执行。
- 仍需：冻结版真实光流/Zarr读写/C2PA签名及取消恢复的完整门槛、
  uvx MCP/升级回滚、依赖清单及第三方许可、三平台实际验收。
  未提交、推送、发布；CodeGraph 索引同步未执行。

## 历史状态：2026-09-19 组合流程实现与本机验收完成

本节优先于下方历史检查点。

- 新增 first_change、compare_media、quality_overview，统一提供 Python run_workflow、
  CLI workflow 与 MCP pixelprobe_workflow；MCP 同步/异步均使用既有有界进程调度器。
- 每个来源单遍顺序读取；首次变化命中后停止。复用解码器、RGB8 绝对差算子、
  亮度运算、资源预算及 Bundle。双来源分别检查白名单、合并计算内存预算并复验输入。
- 明确闭区间范围、采样遗漏、异常时间戳、实际解码/转换数量和 EOF 是否确认。
  视频版本按展示帧号配对，报告时间偏差；不自动缩放、重采样或推断时间同步。
  短来源结束后报告未配对尾部，元数据帧数低估不截断真实解码。
- Bundle 保存逐帧数值表及 validity、变化坐标、完整参数和局限。
  差异图保存原始 uint8 数值，PNG 预览通过 DAG 关联；首次变化的预览关联命中帧对。
  质量候选只覆盖检查到的帧，不给出主观质量分或真伪结论。
- 新增 19 项组合专项通过；包括真实 FFV1、VFR/重复 PTS、尾帧低估、提前停止、
  采样遗漏、双源哈希、取消、磁盘/内存超限、中文 CLI、spawn 与持久任务领取。
  组合与 MCP 接口定向验证 42 passed，真实 stdio 已执行。
  最终全量回归 **420 passed、0 skipped，67.14 秒**；使用 .venv-test，
  测试临时目录位于系统 Temp，禁用 pytest 缓存。最终 diff --check 通过。
- 说明见 [组合分析流程](workflows.md)。流程不具备 checkpoint，不声称可恢复；
  单张/多页图片只分析首张，RGB8 结果不覆盖原生位深、Alpha 或音频。
- 剩余主项：三平台独立程序及 uvx 发行验收（含 C2PA 和组合流程的冻结程序验证）。
  本轮父代理直接实施，未提交、推送或发布；修改后 CodeGraph 索引同步未执行。

## 历史状态：2026-09-19 C2PA 真实签名与信任验证

本节优先于下方历史检查点。

- 新增独立官方 SDK 适配层，依据结构化活动清单状态区分无凭证、验证失败、
  信任未知和受当前策略信任；不再用异常文字猜测无凭证。
- 显式关闭远程清单与 OCSP 获取，保留签名、信任和时间戳验证。远程凭证未获取、
  原生依赖加载失败均标记为未检查；不把离线验证等同于最新吊销状态查询。
- Python、CLI 和 MCP 均支持显式 PEM 信任锚，限制 64 KiB，报告记录策略与指纹。
  C2PA 单项失败写入 Bundle 事件，其他证据方法继续；不自动判断媒体真伪。
- 项目 .venv-test 已安装 c2pa-python 0.37.10；doctor 实跑 healthy=true，
  C2PA status=ok。未修改全局 Python。Linux 测试矩阵已配置 evidence extra。
- 新增真实离线生成签名 JPEG 测试：未知信任、显式根证书信任、内容与清单篡改、
  无效锚、中文路径 CLI、Bundle 完整性、真实 spawn。签名回调由独立密码库复验。
  安装版本 SDK 要求签名证书包含组织字段，测试已补全，未忽略签名错误。
- 补齐原生加载失败、损坏空清单保护后的全量回归为 **399 passed、0 skipped，71.80 秒**。
  随后 MCP 原因提示调整及两项回归加入后，C2PA、证据、MCP 和 worker 定向验证
  **61 passed、0 skipped，17.10 秒**。提示调整后未再次运行全量；最终 diff --check 通过。
  测试禁用 pytest 缓存，临时目录使用系统 Temp 下的项目专用目录。
- 用法与局限见 [C2PA 离线验证](c2pa-verification.md)。本机源码验证不代表
  Linux/macOS 或冻结程序发布验收；C2PA 三平台打包门槛仍待真实执行。
- 剩余主项：组合分析流程、三平台独立程序及 uvx 发行验收。
  本轮父代理直接实施，未提交、推送或发布；修改后 CodeGraph 索引同步未执行。

## 历史状态：2026-09-19 资源控制实现与本机验收完成

本节优先于下方历史检查点。

- 媒体头和执行计划在受限 spawn 子进程解析，按实际尺寸、选择范围、表示和算子估算；
  调度器据计划缩减预留，保持 FIFO 与总额约束。内存包含原生依赖运行开销，
  小裁剪不能绕过完整帧成本；图片元数据读取不再提前完整解码。
- 主进程监控实际 RSS（20 ms 采样，Windows 同时检查峰值），资源超限沿用协作取消、
  2 秒宽限及强制回收流程。任务结果不可领取，后续任务可继续运行。
- 共享预算已接到帧/原生副本、partial、缓存、checkpoint、NPY、PNG、Bundle、Zarr
  的写入；Zarr 按编码后实际字节检查。父进程任务记录和结果另预留 4 MiB 并计入总额。
  实际删除后释放、重命名迁移、旧 checkpoint 原子替换峰值与恢复文件计费均有保护。
- 新增 resource_plan / resource_usage，报告计划、实测内存和文件峰值。
  Bundle 保留两次完整校验，新增次数与读取字节统计。psutil 纳入依赖、doctor 和完整打包配置。
- 新增 23 项资源专项测试，全部通过。包含真实 2001 帧视频只解码 5 帧、
  转换/存储 2 帧的成本断言，以及真实重复 PTS 视频顺序身份与空间预算验证。
  含上述 23 项专项的最终全量回归为 **375 passed、0 skipped，60.81 秒**。
  使用 .venv-test，临时目录位于系统 Temp，禁用 pytest 缓存。
- doctor --json 实际运行 healthy=true，psutil 7.2.2 可用；C2PA 依赖仍明确标记
  unavailable，不影响本资源控制验收，也不代表 C2PA 高级验证已完成。
- 已实现范围及采样限制详见 [资源控制](resource-control.md)。
  不将采样监控宣传为 OS 瞬时硬限制，也不把 Windows 测试视为三平台发行验收。
- 剩余其他主项：C2PA 真实签名和信任验证、组合分析流程、三平台独立程序与 uvx 发行验收。
  本轮父代理直接实施，未提交、推送或发布；修改后 CodeGraph 索引同步未执行。

## 历史状态：2026-09-19 临时文件合计预算

本节优先于下方历史检查点。

- 帧存储、原生图片副本、文件后备采样结果及恢复 partial 共用执行上下文的临时文件预算；写入前合计预留，包含未刷盘数据与 NPY 文件头，避免各文件分别通过但总量超标。
- 根据目标目录所在磁盘检查新增空间；已有恢复文件计入总额，但不重复要求已占用的空间。失败的预留不改变账本，帧文件实际删除后才释放预算。
- 新增 7 项回归测试，覆盖合计超限、NPY 文件头、磁盘不足、已有文件恢复、释放条件、真实 NPY 输出和帧存储关闭；定向测试 53 项通过。最终全量验证：**352 passed、0 skipped，59.27 秒**，使用 .venv-test，临时目录位于系统 Temp，禁用 pytest 缓存。
- 范围限制：这是已接入写入点的应用层预检，不是操作系统磁盘硬配额；其他进程仍可能在检查后占用磁盘。缓存副本、checkpoint 元数据、Bundle/Zarr 最终写入及证据分析尚未全部接入，不能声称全执行链配额已完成。实际进程内存监控与精确调度估算仍待完成。
- 本轮由父代理直接实施；未提交、发布或执行三平台发行验收。

## 历史状态：2026-09-19 独立缓存引用保护与 Zarr 验证

本节优先于下方历史检查点。

- 已创建项目内 .venv-test，复用现有基础依赖并安装声明范围内的 Zarr 3.4.0。全局 Python 未修改；原跳过的 Zarr 并发提交保护测试已实际通过，两项 Zarr 专项测试均通过。
- 独立 CLI/Python 缓存新增跨进程操作引用和数组句柄引用。生成、写入或返回句柄仍在使用缓存时拒绝清理；关闭句柄或进程退出后释放。
- 新增 cache inspect、cache prune；prune 默认预览，--apply 才执行删除。现有 clear 同样执行引用与归属检查。
- 普通缓存按有效元数据识别归属，partial 使用明确程序标记。MCP 新 checkpoint 也带归属标记，支持回收无任务记录、无活动服务引用且已过期的孤立目录。缺标记且无法确认归属的历史数据保留，不自动认领。
- 新增 9 项回归测试，覆盖跨实例/跨进程引用、句柄关闭、异常释放、未知文件保留、到期预览与执行、CLI 默认预览、受管孤立 checkpoint 回收。
- 最终全量验证：**345 passed、0 skipped，59.32 秒**，在 .venv-test 中执行；不是干净环境或 Python 3.12 的发布验收。Zarr 专项 2 passed，缓存/生成/恢复定向测试 65 passed。
- 测试使用系统 Temp 专用目录，未清理用户现有缓存或 Artifact。.venv-test 已加入忽略规则。使用说明见 [缓存维护](cache-maintenance.md)。
- 剩余主项：精确资源估算与运行期内存/磁盘配额、C2PA 真实签名和信任验证、组合分析流程、三平台独立程序与 uvx 发行验收。旧版程序/外部工具未遵守引用协议的情况不能由本版本完整追踪。
- 本轮由父代理直接完成，未调用 Gemini；未提交、发布或执行三平台验收。CodeGraph 用于定位，修改后索引同步未执行。

## 历史状态：2026-09-17 MCP 受管存储维护

本节优先于下方历史检查点。

- 新增 pixelprobe_maintain_storage，默认 dry_run=true，报告受管占用、过期任务、可回收字节、跳过项和阻塞状态。显式 dry_run=false 才删除。
- 过期按终态时间计算，启动时不再按 mtime 直接删记录。保留恢复链引用的 checkpoint；任务文件删除失败时保留可重试索引；目录异常或引用不确定时不推测删除。正式 Bundle 和未知文件不参与清理。
- 活动工作进程与结果领取由管理器锁保护；OS 存储租约阻止其他存活服务实例同时清理，并避免把另一实例的活动任务误判为中断。实际跨进程互斥在 Windows 测试通过，Linux/macOS 实跑未执行。
- CLI cache clear 已禁止直接清理 MCP checkpoint 缓存，避免绕过引用检查。
- 新增 11 项测试，覆盖默认预览、正式 Bundle/未知文件保留、共享恢复引用、活动任务/其他实例、删除失败、损坏记录、重解析点、终态时间、真实进程锁、MCP 会话与 CLI 绕过保护。
- 最终全量验证：**335 passed、1 skipped，61.90 秒**；差异格式检查通过。所有删除测试仅针对测试临时目录，没有对现有用户 Artifact 执行清理。项目根目录没有新增 .pytest-*。
- 用户要求父代理自行完成，本轮未调用 Gemini/Antigravity。修改后 CodeGraph 索引同步、三平台构建与发布未执行；未提交。
- 清理主任务尚有尾项：独立 CLI/Python 缓存目录的活动引用管理，以及无法确定归属的历史孤立缓存处理。精确资源估算/运行期配额、C2PA 信任验证、组合分析、三平台和 uvx 发行验收仍待完成。

## 历史状态：2026-09-17 显式 checkpoint 恢复

本节优先于下方历史检查点。

- 持久化 generate_representation 任务自动保存受管 checkpoint、缓存与恢复请求。新增 pixelprobe_resume_job，仅对中断、取消、超时或失败任务显式恢复，返回新 ID 并通过 resumed_from 保留关联。
- 原执行时限保留；恢复仍经过统一队列和资源预算，同一 checkpoint 的活动任务互斥。重启不自动重跑，没有恢复信息的旧记录明确拒绝。
- 复用引擎的完整输入哈希、请求、计划、算子版本和缓存验证；限制 checkpoint 路径并拒绝目录及缓存中的链接/重解析点。
- 新增 8 项恢复测试，包括写出真实 checkpoint 后中断、重启后显式恢复、真实 spawn 工作者、缺失记录、请求/输入失配、并发恢复拒绝、路径穿越，以及实际 MCP 会话中的恢复与领取。
- 全量验证：**324 passed、1 skipped，55.95 秒**；差异格式检查通过。测试使用系统 Temp 下的 pixelprobe-resume-full-20260917，禁用 pytest 缓存。
- 用户要求父代理自行完成，本轮未调用 Gemini/Antigravity。CodeGraph 已用于定位；修改后索引同步未执行。未提交或发布。
- 使用方式和当前限制见 [MCP 任务与显式恢复](mcp-jobs.md)。checkpoint 缓存暂不自动清理；下一项为活动引用保护和清理。仍待完成精确资源估算/运行期配额、C2PA 信任验证、组合分析、三平台及 uvx 发布验收。

## 历史状态：2026-09-17 资源预算调度

本节优先于下方历史检查点。

- 已接入单任务预算校验、FIFO 资源等待、启动时预留和工作进程退出后的释放；资源等待仍计入排队时限，同步和异步 MCP 均保留明确的 MCP_RESOURCE_LIMIT 错误。
- 新增 9 个回归用例，覆盖内存/临时磁盘准入、FIFO、资源等待取消和超时、无副作用拒绝、两类 MCP 接口和异常释放。全量验证：**316 passed、1 skipped，58.88 秒**。
- 测试禁用 pytest 缓存，临时目录使用系统 Temp 下的 pixelprobe-budget-full-20260917；没有在项目根目录新增 .pytest-*。
- 预算目前依据操作默认估算及只可增加的声明，不等同于实际进程内存硬限制、运行期磁盘配额或按媒体执行计划精算。这些仍需后续完成。
- Antigravity/Gemini 在预检读取 dist/pixelprobe-1.0.0-py3-none-any.whl 时出现 EPERM，执行未开始、无文件改动、无回滚；本轮由父代理实现和验证，Gemini 审查未执行。
- Windows 沙箱 ACL helper 仍无法启动；补丁通过经审批的沙箱外 apply_patch 执行，全量测试同样经审批执行。CodeGraph 已用于定位，修改后索引同步未执行。
- 尚未完成：checkpoint 显式恢复、活动引用清理、精确资源估算及运行期配额、真实 C2PA 信任验证、组合分析流程、三平台和 uvx 发布验收。未提交或发布。

## 历史状态：2026-09-17 回滚恢复复验

本节优先于下方历史检查点。此前协作取消导致多个已修改的受版本控制文件退回发布基线；本次从本地快照 `28884b8` 逐项恢复缺失内容，保留当前其他未提交修改，没有切换分支或重置 Git 历史。

- 已恢复 CLI 的 MCP、doctor、evidence 入口，MCP 参数解析、冻结程序入口、尾帧边界修复、单遍网格抽帧、局部共享帧存储、执行统计及对应测试。
- 已恢复完整版依赖收集与 CI 配置；这仅代表配置恢复，未执行三平台构建、发布或 uvx 升级回滚验收。
- 前一恢复步骤已补回任务工具、证据服务方法、输入模型与协作取消参数。本次完整回归：**307 passed、1 skipped，52.35 秒**。测试目录为 `.pytest-recovery-20260917`。
- 有一条旧 `.pytest_cache` 无写入权限警告；不影响本次测试结果。MCP 参数帮助已运行，差异格式检查通过（Git 有换行规范提示）。
- Antigravity 只读复核因旧测试目录 `.pytest-boundaries` 的访问权限错误，在预检阶段失败；执行未开始、未修改文件、未触发回滚。复核由父代理继续完成。Windows 沙箱另有 ACL 启动故障，本次恢复补丁及验证使用经审批的沙箱外执行。
- 项目规则已同步最新 CodeGraph 优先查询与 Antigravity balanced 策略。

### 下一项与明确未完成项

- 当时总资源预算尚未接入调度；现已完成预留、FIFO 等待和释放，验证结果及限制见顶部最新检查点。
- checkpoint 显式任务恢复、活动引用清理、全链路内存与运行期磁盘配额、C2PA 真实签名样本和信任验证、组合分析流程仍待完成。
- 三平台独立程序、无 Python 环境、uvx 安装升级回滚及正式发行验收尚未完成；未提交或发布本次修改。

## 已实施并通过本机测试

- 估算帧数不再拒绝实际存在的尾帧；实际越界仍报错。
- 网格抽帧统一单遍扫描，更新旧 seek 注释。
- 新增 pixelprobe --mcp 转发入口，保留旧入口。
- 模块入口调用 freeze_support，为冻结程序工作进程准备入口。
- 新增三项回归测试；完整测试 277 passed、1 skipped。
- 增加 uvx 固定 tag 配置、预热、更新和回滚文档。

## 已修改但尚未完成验收

- PyInstaller 收集 MCP、OpenCV、Zarr；CI 与发行构建安装对应 extras。
- 尚未实际构建完整版，也未进行三平台 uvx/独立程序测试。
- 尚未发布 wheel、可执行程序或创建提交。

## 下一检查点

1. 完成第一阶段：验证完整版构建、MCP 真会话、光流/Zarr、依赖清单与许可；补充 wheel 固定发行安装和升级回滚验证。检查延迟加载与 --mcp 帮助可发现性。
2. 新增 doctor 和配置参数；保留白名单。
3. 实施 spawn 工作进程、有界 FIFO 队列（并发 2、容量 16、排队 30 秒）、取消后 2 秒强制终止及资源准入。
4. 新增提交、查询、取消、领取任务工具；持久化状态，重启标记中断，记录保留 7 天，正式 Bundle 不自动清理；旧工具复用调度器。
5. 按需共享解码、峰值预算、磁盘配额、缓存安全清理及 Bundle 校验成本测量。
6. 实施 EXIF/C2PA/ELA/噪声与边缘证据分析、组合流程及三平台基准与交付验证。

## 约束

优先 uvx，同时保留统一完整版；本轮不重写 Rust/Go。
保持已有 CLI、Python API 和 MCP 响应兼容。证据不自动判定真伪。
上述未完成项不能因当前测试通过而视为已交付。

## 第二检查点：配置与诊断

- 实现 MCP --allow-root、--artifact-root，参数优先于环境变量，保持白名单。
- 实现 doctor --json：依赖导入、目录配置、写入探针和磁盘空间检查。
- 能力查询绕开媒体操作槽位；新增忙碌时能力查询回归测试。
- 更新入口参数隔离测试，修复默认结果目录写回环境导致后续启动失败的问题。
- 工作进程调度尚未接入：下一步应把 _run_model 的闭包调用改为可序列化的操作请求，再引入 spawn 与资源调度；不要将线程槽位的简单扩容视为进程调度完成。
- --workers、--queue-size 要与实际调度一起交付，当前未暴露无效配置。
- 第二检查点完整测试：282 passed、1 skipped；doctor JSON 与 MCP 参数帮助已实际运行，CodeGraph 索引已同步。

## 第三检查点：隔离执行原型（尚未接入服务）

- 新增 worker.py：固定只读操作白名单、spawn 进程、错误脱敏、管道传输、取消后回收进程。
- 新增真实 spawn 测试：超时后无遗留子进程，后续像素请求与直接计算结果一致；错误不回显输入。
- 尝试接入 _run_model 后发现真实 stdio 会话像素调用超时（独立 run_read 测试正常）；增加 pipe.poll 不能解决，根因尚未确定。
- 已撤回 server/service 切换，现有 MCP 继续使用原线程槽位，不能宣称线上超时取消已修复。
- 两项旧资源测试依赖 monkeypatch 伪造元数据，不能跨 spawn 继承；正式切换时应把预算逻辑单元测试和真实子进程集成测试分开，不能简单删除断言。
- 下一步先定位 stdio 中子进程启动、发送和接收卡点，验证成功后重新接入；之后才实施 FIFO 队列、总资源预算和任务接口。
- 撤回集成后完整测试：284 passed、1 skipped；索引同步且 diff 检查无错误。

## 第四检查点：Windows stdio 集成修复

- 已定位：无窗口 MCP 宿主使用 Windows 默认 spawn 启动方式时，工作进程无法进入 Python 处理入口；沙箱外同样复现。
- 设置 CREATE_NO_WINDOW 后真实 stdio 测试通过。新增独立 Windows 启动适配器，遵循 CPython spawn 握手，不修改全局 CreateProcess。
- 移除临时打印、faulthandler 和全局启动补丁；只读操作已重新接入 _run_model。
- 服务层超时测试确认工作进程回收、槽位释放和后续像素请求成功；两个依赖父进程 monkeypatch 的预算测试改为服务层单元测试。
- 仍保持单槽位；生成操作仍用线程。FIFO 队列、并发预算、任务持久化、生成取消及完整发行验收尚未完成。
- 此适配器使用 CPython 平台内部接口，后续发行必须覆盖支持的 Python 版本、虚拟环境和冻结程序。
- 本检查点完整测试 284 passed、1 skipped；最后补强的服务槽位回收测试 2 passed。CodeGraph 已同步，diff 检查无错误。

## 第五检查点：任务调度、局部解码与证据分析初版

本节描述当前代码状态，替代第四检查点中“单槽位、生成操作仍用线程”的状态说明。

- 同步媒体工具和新增提交、查询、取消、领取任务接口已接入同一进程调度器；默认并发 2、等待容量 16。新增任务状态持久化、重启中断标记及脱敏轮转诊断日志。
- 共享帧存储合并请求选择，只转换和存储选中帧；明确帧区间在末端停止，输出保留展示帧编号并记录解码、转换数量。
- 时间区间尚未证明来源时间戳单调，保守完整扫描并报告成本，避免后续时间戳回退造成漏帧。该策略不能标记为时间区间性能优化全部完成。
- 新增 evidence CLI/MCP 初版及结果包，包含元数据、C2PA 适配、JPEG ELA、噪声和边缘候选；不输出真伪结论。
- 本次边界修复：像素上限检查先于整图解码，格式复用图片头避免二次解码；源内容变化时拒绝提交证据包；稀疏采样仍验证未采样的区间末端；图片也记录解码及转换数量。
- 验证：完整测试 294 passed、1 skipped（47.61 秒）；随后补充异常时间戳计划回归、恢复原逐帧时间映射断言，相关测试 46 passed。异常时间戳测试验证计划选择，尚非真实异常 PTS 媒体端到端验收。

### 仍须完成，不能视为正式验收通过

- 调度器总资源预算、真正子进程协作取消、持久化失败与取消竞争审计、checkpoint 显式恢复及活动引用保护下的清理。
- 全执行链内存预算与运行期磁盘配额；VFR、异常 PTS、高位深及磁盘写满素材的完整验证。
- C2PA 官方实现真实签名样本与信任状态验证；原始数值热力图、单项无效值语义及组合分析流程。
- 三平台独立程序构建及无 Python 环境真实 MCP、取消、光流、Zarr、C2PA 验收；wheel 发布、uvx 安装升级回滚、校验和和第三方许可交付。

## 第六检查点：调度故障恢复与协作取消

- 提交持久化失败归还队列容量；最终状态提交失败禁止领取结果且始终释放槽位；服务关闭逐项取消排队任务，避免容量重复释放及状态遗留。
- 修复工作进程正常退出误置取消事件的问题。取消与最终状态提交共用锁，取消先到时不发布任务结果。
- 通过 spawn 共享事件向子进程传递取消；生成 API 增加可选 cancellation，复用引擎检查点；证据分析在方法之间及提交前检查取消。原生调用仍无法即时中止，2 秒后保留强制回收。
- 独立版收集 c2pa；doctor 展示该能力。CI 与发行配置增加三平台完整测试、冻结程序真实 MCP 握手及工作进程像素读取。配置已更新，远端构建尚未执行。
- 验证：调度器变更后完整测试 298 passed、1 skipped；协作取消接入后相关测试 72 passed；证据取消及 doctor、工作进程测试 14 passed。完整测试早于协作取消接入，不能等同于最新代码全量复验。
- 尚未完成：总资源预算、严格实时排队超时、checkpoint 任务恢复、活动引用清理、C2PA 真实凭证验证、原始数值热力图、组合分析和实际三平台发行验收。第五检查点清单以本节已完成项为补充，不能将 CI 配置当成验收结果。

## 第七检查点：证据分析数值化与溯源闭环

- 完善 `src/pixelprobe/evidence.py`：给 ELA、局部噪声一致性、边缘候选生成独立数值 DataArtifact（分别为 `tensor_evidence_ela`、`tensor_evidence_noise`、`tensor_evidence_edge`），明确记录坐标空间（storage_pixels）、物理/算法单位、算子参数与适用限制，不宣称传感器原始数据。
- 预览张量（`preview_evidence_ela`、`preview_evidence_noise`、`preview_evidence_edge`）精确溯源到对应数值 DataArtifact，在 ProvenanceGraph 中建立真实 DAG 节点依赖，`BundleReader(..., verify="full")` 完整校验通过。
- 报告张量（`tensor_evidence_report`）引入 `validity` 掩码：失败或不适用的指标（如 PNG 来源下的 ELA）使用 `validity=False` 明确无效，测量值填入 `NaN`，杜绝将默认 0 误判为真实测量。
- 测试覆盖：
  - JPEG 来源包含 4 个 DataArtifact 和 3 个 PreviewArtifact，预览来源追溯到对应数值 DataArtifact；
  - PNG 来源 ELA 标记为 not_applicable，report 张量 validity 第 0 项为 False 且数值为 NaN；
  - 8x8 已知小数组样本验证边缘 RMS（理论值 100/(2*sqrt(2))）、纯色噪声零残差及 ELA 每通道差值数学定义；
  - 保持取消与媒体篡改拒绝提交回归通过。
- 尚未完成：总资源预算、checkpoint 任务恢复、C2PA 官方库真实签名凭证与证书链校验、组合分析流程及三平台独立程序与发行验收。
