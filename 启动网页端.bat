@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] 正在检查 Docker...
where docker >nul 2>nul
if errorlevel 1 (
  echo 未找到 Docker，请先安装 Docker Desktop。
  pause
  exit /b 1
)

docker info >nul 2>nul
if not errorlevel 1 goto docker_ready

echo Docker 尚未运行，正在启动 Docker Desktop...
if not exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
  echo 未找到 Docker Desktop 程序，请手动启动后重试。
  pause
  exit /b 1
)
powershell -NoProfile -Command "Start-Process -FilePath 'C:\Program Files\Docker\Docker\Docker Desktop.exe' -WindowStyle Hidden"

set /a WAIT_COUNT=0
:wait_docker
timeout /t 3 /nobreak >nul
docker info >nul 2>nul
if not errorlevel 1 goto docker_ready
set /a WAIT_COUNT+=1
if %WAIT_COUNT% GEQ 40 (
  echo Docker 启动超时，请检查 Docker Desktop。
  pause
  exit /b 1
)
goto wait_docker

:docker_ready
echo [2/4] 正在启动数据库、队列、后端和网页端...
docker compose up -d --build
if errorlevel 1 (
  echo 项目启动失败，请查看上方错误信息。
  pause
  exit /b 1
)

echo [3/4] 正在等待后端就绪...
set /a HEALTH_COUNT=0
:wait_health
powershell -NoProfile -Command "try { $r=Invoke-RestMethod -Uri 'http://localhost:8000/health' -TimeoutSec 3; if ($r.ok) { exit 0 } }; catch {}; exit 1" >nul 2>nul
if not errorlevel 1 goto health_ready
timeout /t 2 /nobreak >nul
set /a HEALTH_COUNT+=1
if %HEALTH_COUNT% GEQ 30 (
  echo 后端健康检查超时，请运行 docker compose logs backend 查看原因。
  pause
  exit /b 1
)
goto wait_health

:health_ready
echo [4/4] 启动完成，正在打开登录页...
start "" "http://localhost:3000/login"
echo.
echo 网页地址：http://localhost:3000
echo 后端接口：http://localhost:8000
echo.
pause
endlocal
