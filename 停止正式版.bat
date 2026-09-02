@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".env.deploy" (
  echo 未找到 .env.deploy，部署版可能尚未初始化。
  pause
  exit /b 1
)

docker compose --env-file .env.deploy -f docker-compose.deploy.yml down
pause
endlocal
