# uvx 启动、预热与更新

uvx 管理隔离的 Python 环境，不是安全沙箱，也不会消除原生依赖。
首次运行需要联网下载；缓存完整时可复用环境。请先安装 uv。

当前使用仓库固定 tag 作为入口；正式 wheel 发布并验证后，可将 --from
替换为对应的固定发行版本。不要将尚未发布到包索引的版本写入配置。

```json
{
  "mcpServers": {
    "pixelprobe": {
      "command": "uvx",
      "args": [
        "--python", "3.12",
        "--from", "pixelprobe[mcp] @ git+https://github.com/2061863797/PixelProbe.git@v2.1.0",
        "pixelprobe-mcp"
      ],
      "env": {"PIXELPROBE_MCP_ROOTS": "C:/Media"}
    }
  }
}
```

将允许目录改为已有媒体目录；多个目录在 Windows 用分号分隔，在 Unix
用冒号分隔。光流与 Zarr 用户将 extra 改为 `[mcp,flow,storage]`。

连接客户端前，用同一 --from 参数执行 pixelprobe --version 完成环境预热：

```powershell
uvx --python 3.12 --from "pixelprobe[mcp] @ git+https://github.com/2061863797/PixelProbe.git@v2.1.0" pixelprobe --version
```

预热成功仅表示环境与 CLI 可启动，仍须在客户端验证 MCP 连接和真实媒体调用。

更新时停止旧服务，预热并验证新的固定 tag/commit，修改配置后重启。
保留原配置以便回滚，不要删除整个 uv 缓存或覆盖运行中的程序。
正式发行不提供包含第三方原生库的独立程序；开发者自己的本地单文件构建应使用
不同版本目录，退出旧进程后再切换客户端程序路径。

源码开发版已新增 `pixelprobe --mcp`；上面的已发布 tag 配置继续使用
兼容入口 `pixelprobe-mcp`，不假定历史版本具有新参数。

## 开发版目录配置与诊断

新版入口支持以下命令（历史 tag 未必具备这些参数）：

```powershell
pixelprobe doctor --json --allow-root C:/Media
pixelprobe --mcp --allow-root C:/Media --artifact-root C:/Media/results
pixelprobe --mcp --allow-root C:/Media --workers 2 --queue-size 16
```

`--allow-root` 可重复指定，显式参数优先于环境变量；结果目录必须位于
允许目录内。无参数时继续读取已有环境变量，未配置时只允许当前目录。
`doctor` 检查依赖能否导入、目录配置和磁盘空间，并在结果目录最近的
现有父目录创建后立即删除写入探针，不创建结果目录。
`healthy` 表示核心依赖与配置正常；可选依赖缺失在 checks 中显示为
unavailable，不能把 healthy 当作全部功能或实际解码已经验证。
核心或配置检查失败时退出码为 1，JSON 中仍保留完整诊断结果。
