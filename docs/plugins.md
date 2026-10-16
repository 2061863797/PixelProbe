# Claude Code 与 Codex 插件

插件把 PixelProbe 的 MCP 服务和 5 个场景 Skill 打包在一起，安装后 AI 助手会在合适的任务里自动使用：

| Skill | 用途 |
|---|---|
| `pixelprobe-core` | 通用打法：overview → find → look → measure，地址写法与结果编号 |
| `ui-recording` | 录屏与 UI：动画时长、卡顿掉帧、控件何时变化、截图对比、颜色与位置 |
| `long-video` | 长视频：建索引、镜头切换、黑场闪帧、相似画面、自适应切片 |
| `quality` | 质量与异常：模糊、块效应、冻结重复、闪烁、条纹、码率骤降 |
| `forensics` | 取证线索：EXIF、C2PA、ELA/噪声/边缘、视频码流结构，不判定真伪 |

插件目录在 `plugins/pixelprobe/`，仓库根目录同时提供两边的市场文件，所以可以直接从 GitHub 仓库安装。

## 前提

- 安装 [uv](https://docs.astral.sh/uv/)：插件用 `uvx` 下载并启动固定版本（`v2.0.0` 标签）的 PixelProbe，
  首次启动需要联网，之后复用缓存。可先预热：

  ```powershell
  uvx --python 3.12 --from "pixelprobe[mcp] @ git+https://github.com/2061863797/PixelProbe.git@v2.0.0" pixelprobe --version
  ```

- 插件只能读取允许目录内的媒体（见下文"允许的目录"）。

## Claude Code

```bash
claude plugin marketplace add 2061863797/PixelProbe
```

```bash
claude plugin install pixelprobe@pixelprobe
```

也可以在 Claude Code 里输入 `/plugin` 打开插件界面安装。安装时会提示填写"允许分析的媒体目录"
（多个目录 Windows 用分号、macOS/Linux 用冒号分隔），留空表示只允许当前项目目录；之后可在 `/config` 里修改。

## Codex

```bash
codex plugin marketplace add 2061863797/PixelProbe
```

```bash
codex plugin add pixelprobe@pixelprobe
```

Codex 下 MCP 服务名为 `pixelprobe-plugin`，允许目录为启动 Codex 的当前目录。

## 已经手动配置过 PixelProbe MCP？

如果你之前在 Codex 的 `~/.codex/config.toml` 或 Claude Code 的 MCP 设置里手动加过名为 `pixelprobe` 的服务器，
安装插件后会出现两套工具。手动配置的旧版本工具名与 2.0 不同，建议删除手动配置，只保留插件。
（Codex 插件的服务名特意用 `pixelprobe-plugin`：同名时手动配置会把插件的服务器整个遮住。）

## 更新

插件固定启动 `v2.0.0`；发布新版本时同时更新插件里的版本号与启动命令，然后执行
`claude plugin update pixelprobe@pixelprobe` 或 `codex plugin marketplace upgrade pixelprobe` 后重新安装。

## 验证记录（2026-10-16，Windows）

- `claude plugin validate` 对插件与市场文件均通过；`claude plugin details` 识别出 5 个 Skill 和 1 个 MCP 服务，
  常驻上下文约 460 token。
- Codex 0.162.1：从本地市场安装成功，会话中 5 个 Skill 以 `pixelprobe:*` 出现；MCP 用本地源码启动时
  10 个工具全部出现（`mcp__pixelprobe_plugin_*`）。测试后已卸载并确认 `config.toml` 恢复原样。
- 未执行：以 `v2.0.0` 标签通过 uvx 冷启动（标签在发布时创建）；Claude Code 命令行会话内的实际调用
  （本机命令行登录已过期）；macOS/Linux。
