# 发布与验收记录

只记录各版本的发布状态、验收结果和仍然有效的结论；逐步实施过程见 Git 历史
（`git log -p -- docs/optimization-progress.md` 可查看本文件 2026-10-17 前的完整检查点）。

## 2.1.1（2026-10-17 已发布）

- 发布：标签 `v2.1.1`（提交 2fe002e），https://github.com/2061863797/PixelProbe/releases/tag/v2.1.1 。
  发布工作流完整测试 605 passed，构建与 Release 成功；推送 main 的 CI 在 Linux Python 3.11/3.12/3.13 全部通过。
- 发行物（与 SHA256SUMS.txt 一致）：
  - `pixelprobe-2.1.1-py3-none-any.whl` `6cafb713124d578ba7965f9f695166c62b84f23d8b25a5dbc5a13783506c9ba0`
  - `pixelprobe-2.1.1.tar.gz` `440783f9229b7977e468f1d57267e003aaa2efe3fbde488bad9ec79989d26de5`
- 修复 2.1.0 复测遗留问题：
  - capabilities 清单把 MCP 工具的顶层参数（generate 的 `requests`、find 的 `query`）列在 `arguments` 下，
    不再写在 `options` 里导致照清单调用必失败；对等测试核对清单与工具参数一致。
  - 插件此前只安装 `pixelprobe[mcp]`，环境里没有 OpenCV、Zarr、C2PA，光流、Zarr 输出与 C2PA 验证对所有插件用户
    都不可用；现改为 `pixelprobe[mcp,flow,storage,evidence]`，缺依赖提示同时说明 pip 与 uvx/插件两种方式。
  - `pixelprobe_job` 不传 `job_id` 时给出可读错误，不再返回 pydantic 原始校验信息。
- 真实标签验证（Windows，Python 3.12）：按插件的 extra 用 uvx 空缓存冷启动 49 秒（含从 GitHub 拉取构建）、
  缓存后 2 秒；`doctor` healthy，cv2、zarr、c2pa 均可用。

## 2.1.0（2026-10-17 已发布）

- 发布：标签 `v2.1.0`（提交 3dbd131），https://github.com/2061863797/PixelProbe/releases/tag/v2.1.0 。
  发布工作流的完整测试 600 passed，构建与 Release 成功；推送 main 的 CI 在 Linux Python 3.11/3.12/3.13
  全部通过，wheel 隔离安装通过。
- 发行物（与 SHA256SUMS.txt 一致）：
  - `pixelprobe-2.1.0-py3-none-any.whl` `3373ecae427b50efb50fa801f4ee0abc4bc0d15ef648b16cbfc157845e2e9bfa`
  - `pixelprobe-2.1.0.tar.gz` `d70827e17330d5c67cd2110db1fa8fd92f4837d0eef77da83eb5296a350d6742`
- 真实标签验证（Windows，Python 3.12）：uvx 空缓存冷启动 15 秒、缓存后 2 秒，版本 2.1.0。
- 本地候选验收（发布前）：干净虚拟环境安装完整 extra（`doctor` healthy、`find` 结果正确）；uvx 本地 wheel
  离线运行 `tests/test_distribution.py` 4 passed；2.0.0 → 2.1.0 → 2.0.0 升级回滚三阶段均 10 个工具、像素核对通过。
- 未执行：插件在 Claude Code / Codex 中从 GitHub 重新安装后的会话调用；macOS 实机运行。

### 本版修复的问题（来自 2026-10-17 CLI 与 MCP 实测）

对同一素材、同一参数分别经 CLI 与真实 MCP stdio 调用 28 项能力并比较，发现并修复：

| 编号 | 问题 | 处理 |
|---|---|---|
| H1～H3 | 1080p 以上视频被 MCP 中途停止或预检误拒（估算偏低/偏高、默认上限漏算运行时余量、复用进程沿用上一任务的终身峰值） | 内存按本机自动分配；估算只用于排队和事先拒绝，停止线改为任务额度；只计本任务期间峰值；内存失败独占重试一次；整机内存监视 |
| — | 1080p `reduce`/`spectrum` 两侧都报物化超限 | 只被下游消费的整段帧中间结果落到临时文件；引擎单步上限跟随任务额度 |
| M1、L1～L3 | 输出重名报路径白名单错误；坏文件、未知 artifact 报泛化错误；领取已取消任务提示不准 | `BUNDLE_TARGET_EXISTS`、`FILE_NOT_FOUND`、`UNSUPPORTED_MEDIA`、`ARTIFACT_NOT_FOUND`，按任务状态说明能否领取 |
| M2 | 默认索引目录不能同目录改名（WinError 17） | 退回逐文件复制提交，其他写入失败提示设置 `PIXELPROBE_INDEX_DIR` |
| M3、M4、L4、L5、L7、L8 | EXIF 只给字段名、`info` 两侧不一致、选项与返回字段不对等 | 两侧返回 EXIF 值（GPS 需显式开启）、`info` 共用能力、MCP 增 `no_index`；对等测试锁定字段一致 |

实测结果：本机自动预算约 8 GB 时，2K、4K 的单帧、变化检测、扫描、光流、时间线、质量总览、建索引均通过。

## 2.0.0（2026-10-16 已发布）

- 发布：标签 `v2.0.0`，https://github.com/2061863797/PixelProbe/releases/tag/v2.0.0 。
  wheel `1ef2a5e97062dfbc9e42b801909c657a548b7a71c4a94c7d94a3e6d755774b60`，
  sdist `bd4f50bf573b8c9a0deb074acfdd10f06ec0ae2861d05adacd1063317f236f6b`。
- 内容：统一地址写法、能力登记表与 CLI/MCP 对等测试、MCP 精简为 10 个工具、媒体索引、`find` 搜索语言、
  新切法、码流结构、Claude Code 与 Codex 插件（5 个 Skill），迁移见 [迁移到 2.0](migration-2.0.md)。
- 验证：uvx 冷启动与 `tests/test_distribution.py` 4 passed；v1.1.0 → 2.0.0 → v1.1.0 升级回滚通过；
  Codex 从 GitHub 安装插件后 5 个 Skill 与 10 个工具出现；`claude plugin validate` 通过。
- 插件格式要点：Codex 0.162.1 读取兼容层（`.codex-plugin` + 旧格式 MCP 文件），且同名手动配置会遮住插件服务，
  因此 Codex 侧服务名为 `pixelprobe-plugin`，两种格式都提供。

## 1.1.0（2026-09-20）：发行边界

- 正式 Release 只发布 wheel、sdist 和校验和，不再构建或上传包含第三方原生库的冻结程序；
  开发者可用 `scripts/build_local_standalone.py` 生成仅供本地使用的单文件程序（Windows 实测构建与冻结版 CLI/MCP 验收通过）。
- Windows 专项修复：Zarr 写入与校验句柄在提交前关闭，关闭后目录重命名短暂被拒时有限重试；`doctor` 写入探针显式命名并主动删除。

## 仍然有效的验证结论

- **资源控制**（2026-09-19 起，2.1.0 改为按本机预算）：媒体头与执行计划在受限子进程解析；主进程以 20 ms 采样监控
  工作进程实际内存（Windows 同时检查本任务期间峰值），超限沿用协作取消、2 秒宽限与强制回收，任务结果不可领取，
  后续任务可继续；预算覆盖帧副本、partial、缓存、checkpoint、NPY、PNG、Bundle 与 Zarr 写入。
  采样监控不是操作系统级瞬时硬限制。说明见[资源控制](resource-control.md)。
- **C2PA**（2026-09-19，Windows）：官方 SDK 适配层区分无凭证、验证失败、信任未知和受信任；关闭远程清单与 OCSP 获取；
  支持显式 PEM 信任锚（≤ 64 KiB）。真实离线签名测试覆盖未知信任、显式根证书信任、内容与清单篡改、无效锚、
  中文路径 CLI 与真实 spawn。离线验证不等于最新吊销状态。说明见 [C2PA 离线验证](c2pa-verification.md)。
- **组合流程**（2026-09-19）：每个来源单遍顺序读取，首次变化命中即停；视频版本按展示帧号配对，不自动缩放、
  重采样或推断时间同步；无 checkpoint。说明见[组合分析流程](workflows.md)。
- **平台范围**：以上均为 Windows 实测；Linux 由 CI 运行全量测试；macOS 未实机运行。
