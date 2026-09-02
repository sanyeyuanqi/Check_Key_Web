@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo Key Control 2.0.0 部署程序
echo.

where docker >nul 2>nul
if errorlevel 1 (
  echo 未找到 Docker，请先安装并启动 Docker Desktop。
  pause
  exit /b 1
)

docker info >nul 2>nul
if errorlevel 1 (
  echo Docker 尚未运行，请先启动 Docker Desktop。
  pause
  exit /b 1
)

if not exist ".env.deploy" (
  echo 首次部署，正在生成随机数据库密码、管理员密码和加密密钥...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0初始化部署配置.ps1"
  if errorlevel 1 (
    echo 部署配置生成失败。
    pause
    exit /b 1
  )
  echo.
)

echo [1/2] 正在拉取并构建最新稳定镜像...
docker compose --env-file .env.deploy -f docker-compose.deploy.yml build --pull
if errorlevel 1 (
  echo 镜像构建失败。
  pause
  exit /b 1
)

echo [2/2] 正在启动部署版服务...
docker compose --env-file .env.deploy -f docker-compose.deploy.yml up -d
if errorlevel 1 (
  echo 服务启动失败。
  pause
  exit /b 1
)

docker compose --env-file .env.deploy -f docker-compose.deploy.yml ps
echo.
echo 部署完成。默认网页地址：http://localhost:3000
pause
endlocal
