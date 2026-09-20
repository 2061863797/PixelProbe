# 发行入口验收

正式 GitHub Release 只发布 Python wheel、源码包和校验和，不生成或上传包含第三方
原生库的冻结程序。源码回归通过不等于 uvx 或开发者本地冻结程序已验收。
测试环境需要安装 dev、storage、evidence extra；生成测试签名只使用临时测试密钥。

## 本地单文件程序

使用 `scripts/build_local_standalone.py --output <新目录>` 从当前 Python 环境打包。
输出只供本地使用；脚本拒绝覆盖目录，并默认执行下面的冻结程序真实测试。

```powershell
$env:PIXELPROBE_TEST_EXECUTABLE = (Resolve-Path "候选目录/pixelprobe.exe").Path
python -m pytest tests/test_mcp_server.py tests/test_distribution.py -k "stdio_transport_handshake or distribution"
```

Linux/macOS 使用同名环境变量指向绝对可执行文件路径。本地构建脚本包含上述门槛；
仓库 CI 和正式发布工作流不构建冻结程序。

## uvx 本地 wheel

先使用固定 wheel、Python 3.12 和所有完整版 extra 预安装；完整下载后再验证离线运行。
PIXELPROBE_TEST_COMMAND 是测试启动器的 JSON 数组，不是产品配置项。
该变量优先于 PIXELPROBE_TEST_EXECUTABLE，测试其他入口前应清除它。

```powershell
$env:PIXELPROBE_TEST_COMMAND = ConvertTo-Json -Compress -InputObject @(
  "uvx", "--offline", "--python", "3.12", "--from",
  "pixelprobe[mcp,flow,storage,evidence] @ file:///C:/候选目录/pixelprobe-1.0.3-py3-none-any.whl",
  "pixelprobe"
)
python -m pytest tests/test_distribution.py
```

本地候选与远程同版本 wheel/tag 不一定内容相同。验收必须记录准确文件路径和 SHA256，
不得用本地候选通过代替远程发行物通过；跨版本升级与回滚需要独立演练。

## 实际断言

- CLI 根据请求中的 output.format="zarr" 生成含 Zarr 数组的 Bundle，
  校验 manifest 与全部哈希、shape/dtype，并逐值比较 RGB 数据；再由目标 CLI 完整验证。
- MCP 对无运动视频执行 Farneback 光流，写入 Zarr Bundle，再通过目标进程读取矢量，
  检查 shape 和零运动容差；不能只依靠 doctor 的导入结果。
- MCP 验证测试中真实生成的 C2PA 签名与显式信任锚，独立验证输出 Bundle。
- 单工作槽位下等待任务处于 running 且工作进程已返回资源计划，再取消；
  断言任务变为 cancelled、不可领取结果、无正式 Bundle，并能再次正确读取视频像素。
- 中文输入路径和结果父目录贯穿上述流程。MCP output_name 沿用安全 ASCII 文件名限制。

CLI/MCP 的 generate 仍产生 Bundle。format="zarr" 选择包内 Zarr 存储；
默认继续使用 NPY，已有 memory/npy 请求在这两个入口的 Bundle 封装行为保持不变。

强制超时测试只挂起本测试启动的媒体 worker，使它无法协作退出。
收到 MCP_TIMEOUT 后先独立确认进程不存在，再发起正常媒体读取；
测试自身的兜底清理在断言之后执行，不能代替产品回收。

## 升级回滚演练

scripts/verify_uvx_upgrade.py 接收 --old、--new 两个实际版本不同的 wheel，
以及 --cache 指向已下载依赖的 uv 缓存。使用固定 Python 3.12 与 --offline，
逐个停止并启动旧版、新版、回滚旧版的 MCP，分别核对像素值；
预安装期间临时配置不变，结束后核对旧配置和两个 wheel 的 SHA256。
脚本只使用临时客户端配置，不修改用户实际 MCP 配置。

2026-09-20 已在 Windows 验证本地 v1.0.2 标签重建 wheel → 当前 1.0.3 候选 →
v1.0.2。这是本地 wheel 演练，不代表远程发行物和用户实际客户端均已验收。

## 发行附件

正式工作流构建 wheel 和 sdist，通过 twine 检查并在干净虚拟环境安装 wheel；
标签 Release 只上传这些 Python 包及顶层 SHA256SUMS.txt。

以下原生清单流程保留给本地审计，以及未来重新评估独立版再分发时使用：

scripts/build_release_inventory.py --executable <exe> --output <新目录> --wheel-dir <固定wheel目录>
根据完整版 extra 的实际运行时依赖闭包生成 dependencies.json、
runtime-requirements.txt、THIRD-PARTY-NOTICES.txt、licenses/ 和 SHA256SUMS.txt，
并包含项目文档及许可。生成器拒绝覆盖已有目录；缺失许可时明确失败。
随后将对应 exe 复制到目录根部，再归档；SHA256SUMS.txt 已包含该 exe 的指纹。

独立版使用 packaging/release-constraints.txt 固定 PyAV 18.1.0、C2PA Python
0.37.10 和 OpenCV 5.0.0.93。packaging/native-sources.json 保存三个目标平台的
官方 wheel SHA256、上游构建提交、原生发布包和 Windows FFmpeg 组件源码指纹。
传入 --wheel-dir 后，生成器先验证当前平台三个官方 wheel 的文件哈希，再按 wheel
RECORD 逐项核对已安装运行文件；`.a`/`.lib` 仅为编译链接输入，不计入运行文件。

若未来恢复独立版公开发行，必须再传 --require-redistribution-review。当前清单仍有
原生传递许可核对项，redistribution_review_complete=false，因此该参数以非零状态
退出。当前正式工作流完全不调用这条独立版路径。

C2PA 0.37.10 wheel 未附许可，packaging/licenses/ 中保存了对应
[官方版本的 MIT](https://raw.githubusercontent.com/contentauth/c2pa-python/v0.37.10/LICENSE-MIT)
和 [Apache](https://raw.githubusercontent.com/contentauth/c2pa-python/v0.37.10/LICENSE-APACHE)
原文；SOURCE.json 绑定来源与 SHA256，版本变化或内容变化不能静默复用。

该清单覆盖 Python 包附带或补充的许可、CPython、PyInstaller、关键 wheel 来源和
已安装运行文件一致性；仍不是原生库嵌入的全部传递许可原文或再分发审核结论。
无 Python 的全新系统验收不再是当前 Python 包发行门槛。本机历史冻结候选仍仅作为
本地功能证据；Linux/macOS 的冻结程序真实验收按用户要求暂缓。
