# MCP 任务与显式恢复

组合分析使用 operation=workflow，载荷及覆盖语义见 [组合分析流程](workflows.md)。
与现有任务共用排队、取消和结果领取；组合流程没有 checkpoint，不支持显式恢复。

长时间生成操作使用 `pixelprobe_submit_job`，传入
`operation="generate_representation"` 和原生成工具的完整参数对象。
返回的 `job_id` 用于查询、取消和领取结果。

## 从 checkpoint 恢复

持久化生成任务自动在 Artifact 根目录的
`.checkpoints/<checkpoint_id>/` 保存 checkpoint 和对应缓存；
任务请求单独保存在 `.jobs/` 下，不写入诊断日志。
尚未写出 checkpoint 就被取消的任务无法恢复。

1. 调用 `pixelprobe_get_job(job_id)` 检查原任务状态。
2. 对中断、取消、超时或失败的生成任务，调用
   `pixelprobe_resume_job(job_id)`。
3. 使用返回的新 `job_id` 查询并领取恢复结果。

原任务保留终态，新任务通过 `resumed_from` 关联原任务，并沿用原执行时限。
恢复任务仍经过同一 FIFO 队列和资源预算检查。
同一个 checkpoint 已有活动任务时返回 `MCP_RESUME_BUSY`。
服务重启只将未完成的任务标记为 `interrupted`，不会自动重新执行。

恢复只支持生成操作，且要求原请求记录、checkpoint 和必要缓存仍存在。
已有 CLI/Python API 的 checkpoint 功能保持不变。
旧版任务记录没有恢复信息时返回 `MCP_RESUME_UNAVAILABLE`；
输入内容、请求或算子版本发生变化时，引擎拒绝恢复。
自定义输出名称已存在时也会拒绝，避免覆盖已有 Bundle。

## 限制与维护

同步生成工具不保存可恢复任务记录。任务记录默认保留 7 天；
正式 Bundle 不随任务记录删除。调用
`pixelprobe_maintain_storage()` 预览存储占用、过期任务数量与可回收字节；
显式传入 `dry_run=false` 才执行清理。过期依据任务完成时间计算，
启动时不再仅凭文件修改时间直接删除记录。
仍有保留任务引用的 checkpoint 不会删除，活动任务或其他存活服务实例存在时拒绝清理。
目录或记录异常时保留相关引用并报告 skipped；未知文件和无法确定归属的孤立缓存不删除。
独立 CLI/Python 缓存使用自身的引用保护和 cache inspect/prune，见 [缓存维护](cache-maintenance.md)；
CLI clear/prune 不能删除 MCP checkpoint 缓存。有明确程序归属标记、无任务引用且已过期的
孤立 checkpoint 也可由 MCP 维护接口清理；无法确认归属的历史目录保留。
恢复前会检查缓存目录中的链接与重解析点，
大缓存目录因此有额外检查成本。

恢复保证复用已验证的 checkpoint 数据；它不保证每种算子都能避免重新解码。
MCP 已接入工作进程内存监控、共享写入预算及实际执行计划回报。
查询任务时可查看 resource_plan 和 resource_usage；默认预留、配置方式和
采样监控边界见 [资源控制](resource-control.md)。
