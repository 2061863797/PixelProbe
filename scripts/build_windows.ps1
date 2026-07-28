# Windows 构建脚本：安装、测试、打包 wheel
# 用法：在仓库根目录执行  powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

Write-Host "== 1/4 清理旧构建产物 =="
$ProjectPrefix = $ProjectRoot.TrimEnd("\") + "\"
$GeneratedDirectories = @(
    (Join-Path $ProjectRoot "build"),
    (Join-Path $ProjectRoot "dist"),
    (Join-Path $ProjectRoot "src\pixelprobe.egg-info"),
    (Join-Path $ProjectRoot ".pytest-temp-build")
)
foreach ($Directory in $GeneratedDirectories) {
    if (-not (Test-Path -LiteralPath $Directory)) {
        continue
    }
    $ResolvedDirectory = (Resolve-Path -LiteralPath $Directory).Path
    if (-not $ResolvedDirectory.StartsWith(
        $ProjectPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "拒绝清理仓库外路径：$ResolvedDirectory"
    }
    Remove-Item -LiteralPath $ResolvedDirectory -Recurse -Force
}

Write-Host "== 2/4 安装（可编辑模式 + 开发依赖）=="
python -m pip install -e ".[dev]" --quiet
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }

Write-Host "== 3/4 运行测试 =="
python -m pytest tests --basetemp .pytest-temp-build
if ($LASTEXITCODE -ne 0) { throw "测试未通过，终止打包" }

Write-Host "== 4/4 构建 wheel 与 sdist =="
python -m pip install build --quiet
python -m build
if ($LASTEXITCODE -ne 0) { throw "构建失败" }

Write-Host "完成。产物位于 dist\ 目录。"
