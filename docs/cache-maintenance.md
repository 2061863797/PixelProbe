# 本机缓存维护

独立 CLI/Python 缓存的生成操作、写入操作和返回的数组句柄均持有跨进程引用。
其他实例尝试清理时，如果仍有引用，返回 CACHE_IN_USE。读取结束后请关闭数组句柄：

```python
cached = cache.get(key)
if cached.handle is not None:
    with cached.handle as handle:
        values = handle.materialize()
```

生成返回的张量若使用缓存文件，也需要在不再读取时关闭其 data 句柄。
进程退出会释放操作系统锁；遗留引用文件不会永久阻止清理。

## CLI

```powershell
pixelprobe cache inspect --cache-dir C:\work\cache --json
pixelprobe cache prune --cache-dir C:\work\cache --older-than-days 7 --json
pixelprobe cache prune --cache-dir C:\work\cache --older-than-days 7 --apply --json
```

inspect 报告可确认归属的条目数、占用和跳过项。
prune 默认预览，只有 --apply 才删除；保留时间按目录及其内容的最近修改时间计算。
已有 cache clear 仍可使用，但也会检查引用、链接及归属。
Python 对应接口为 cache.maintain(dry_run=True, max_age_seconds=7 * 86400)。

只有符合缓存元数据结构的条目，或带程序归属标记的 partial，才会进入删除候选。
缺少标记的历史 partial、未知文件、含链接或重解析点的目录均保留并计入 skipped。
扫描达到限制时停止继续处理；返回的统计可能只覆盖已扫描范围。
这些保护仅适用于遵守本版本引用协议的进程，不能追踪旧版程序或外部工具打开的文件。

## MCP checkpoint 与孤立数据

MCP checkpoint 使用 pixelprobe_maintain_storage，CLI clear/prune 不允许绕过它。
新 checkpoint 目录带有程序归属标记。如果任务记录已经不存在，且整个目录都已超过
任务保留期、没有活动实例和可疑记录，就可以作为有明确归属的孤立目录清理。

无法确认归属的历史目录会保留。不会仅凭目录名称或文件年龄自动认领并删除。
正式 Bundle 不参与上述维护。

## 含 Zarr 的本机测试环境

本项目的 .venv-test 环境复用当前 Python 已安装的基础包，并单独安装 storage 所需的
Zarr；它不是干净安装或 Python 3.12 的发布验收环境。运行测试时显式使用：

```powershell
.\.venv-test\Scripts\python.exe -m pytest -rs -p no:cacheprovider
```

全局 python 仍可能没有 Zarr，不能把两个环境的测试结果混用。
