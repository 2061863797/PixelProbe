# Windows 构建脚本：安装、测试、打包 wheel
# 用法：在仓库根目录执行  powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "== 1/3 安装（可编辑模式 + 开发依赖）=="
python -m pip install -e ".[dev]" --quiet
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }

Write-Host "== 2/3 运行测试 =="
python -m pytest tests
if ($LASTEXITCODE -ne 0) { throw "测试未通过，终止打包" }

Write-Host "== 3/3 构建 wheel =="
python -m pip install build --quiet
python -m build --wheel
if ($LASTEXITCODE -ne 0) { throw "构建失败" }

Write-Host "完成。产物位于 dist\ 目录。"
