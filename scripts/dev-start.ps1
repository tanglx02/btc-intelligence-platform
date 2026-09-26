# ==========================================================
# BTC 全市场智能研究平台 - 一键启动开发环境 (Windows PowerShell)
# 用法: powershell -ExecutionPolicy Bypass -File .\scripts\dev-start.ps1
# 注意: PowerShell 5.1 不支持 '&&'，命令使用 ';' 分隔
# ==========================================================
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

Write-Host "==> [1/4] 检查环境变量文件" -ForegroundColor Cyan
if (-not (Test-Path ".env")) {
    Write-Host "    未找到 .env，从 .env.example 复制一份"
    Copy-Item ".env.example" ".env"
}

Write-Host "==> [2/4] 启动基础设施 (Postgres + Redis + Adminer)" -ForegroundColor Cyan
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

Write-Host "==> [3/4] 安装依赖" -ForegroundColor Cyan
Push-Location backend; uv sync --extra dev; Pop-Location
Push-Location frontend; pnpm install; Pop-Location

Write-Host "==> [4/4] 启动后端与前端开发服务器 (新窗口)" -ForegroundColor Cyan
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$RootDir\backend'; uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$RootDir\frontend'; pnpm dev"

Write-Host ""
Write-Host "开发环境已启动:" -ForegroundColor Green
Write-Host "  - Backend API : http://localhost:8000  (docs: /docs)"
Write-Host "  - Frontend    : http://localhost:3000"
Write-Host "  - Adminer     : http://localhost:8080"
Write-Host "后端与前端分别在独立窗口运行，关闭窗口即可停止对应服务。"
