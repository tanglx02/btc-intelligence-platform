# ==========================================================
# BTC 全市场智能研究平台 - 一键启动开发环境 (Windows PowerShell)
# 用法: powershell -ExecutionPolicy Bypass -File .\scripts\dev-start.ps1
# 注意: PowerShell 5.1 不支持 '&&'，命令使用 ';' 分隔
# ==========================================================
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

function Write-Step($Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok($Message)   { Write-Host "[INFO] $Message" -ForegroundColor Green }
function Write-Warn2($Message){ Write-Host "[WARN] $Message" -ForegroundColor Yellow }
function Fail($Message) {
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    exit 1
}

# ---------- [0/5] 依赖检查 ----------
Write-Step "[0/5] 检查依赖"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "未安装 docker，请先安装 Docker Desktop" }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker 未运行，请先启动 Docker Desktop" }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail "未安装 uv，请访问 https://docs.astral.sh/uv/ 安装" }
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    Write-Warn2 "未安装 pnpm，尝试使用 corepack 启用"
    corepack enable pnpm
    if ($LASTEXITCODE -ne 0) { Fail "pnpm 启用失败，请手动安装: npm install -g pnpm" }
}

Write-Step "[1/5] 检查环境变量文件"
if (-not (Test-Path ".env")) {
    Write-Host "    未找到 .env，从 .env.example 复制一份"
    Copy-Item ".env.example" ".env"
}

Write-Step "[2/5] 启动基础设施 (Postgres + Redis + Adminer)"
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# 等待 postgres healthy（最多 120s）
$Elapsed = 0
while ($true) {
    $Status = ""
    try {
        $Status = docker inspect -f "{{.State.Health.Status}}" btc_postgres 2>$null
    } catch { $Status = "starting" }
    if ($Status -eq "healthy") { break }
    $Elapsed += 2
    if ($Elapsed -ge 120) { Fail "等待 PostgreSQL 就绪超时（120s），请查看日志: docker compose logs postgres" }
    Start-Sleep -Seconds 2
}
Write-Ok "PostgreSQL 已就绪"

Write-Step "[3/5] 安装依赖"
Push-Location backend; uv sync --extra dev; Pop-Location
Push-Location frontend; pnpm install; Pop-Location

Write-Step "[4/5] 执行数据库迁移（幂等，可重复执行）"
Push-Location backend; uv run alembic upgrade head; Pop-Location
Write-Ok "如需种子数据: cd backend; uv run python scripts/seed_data.py"

Write-Step "[5/5] 启动后端与前端开发服务器 (新窗口)"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$RootDir\backend'; uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$RootDir\frontend'; pnpm dev"

Write-Host ""
Write-Host "开发环境已启动:" -ForegroundColor Green
Write-Host "  - Backend API : http://localhost:8000  (docs: /docs)"
Write-Host "  - Frontend    : http://localhost:3000"
Write-Host "  - Adminer     : http://localhost:8080"
Write-Host "后端与前端分别在独立窗口运行，关闭窗口即可停止对应服务。"
