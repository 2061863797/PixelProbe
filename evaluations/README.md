# MCP 评估

先在仓库根目录生成确定性素材：

```bash
python scripts/generate_mcp_evaluation_fixtures.py
```

然后让 MCP 客户端以仓库根目录为工作目录启动 `pixelprobe-mcp`，并使用
`pixelprobe_mcp.xml` 中的 10 个只读问题进行评估。素材由程序生成且经过逐像素回读
校验，不纳入 Git 跟踪；重新生成不会改变答案。
