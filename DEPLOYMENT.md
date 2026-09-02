# Key Control 2.0.0 部署说明

此部署版采用前后端分离架构：Next.js 前端、FastAPI 后端、PostgreSQL 18、Redis 8 和独立后台 Worker。

## 快速部署

1. 安装并启动 Docker Desktop。
2. 双击 `部署正式版.bat`。
3. 首次启动会自动生成 `.env.deploy`，并在窗口中显示一次管理员账号和随机密码，请立即保存。
4. 默认访问 `http://localhost:3000`。

## 部署到服务器

首次运行前，可执行以下命令生成配置：

```powershell
powershell -ExecutionPolicy Bypass -File .\初始化部署配置.ps1 `
  -FrontendUrl "https://keys.example.com" `
  -BackendUrl "https://api.keys.example.com" `
  -AdminUsername "admin"
```

使用 HTTPS 时，将 `.env.deploy` 中的 `SESSION_COOKIE_SECURE` 改为 `true`。PostgreSQL 和 Redis 在部署版中不对宿主机开放端口，只有网页端和后端端口可访问。

## 常用命令

```powershell
docker compose --env-file .env.deploy -f docker-compose.deploy.yml ps
docker compose --env-file .env.deploy -f docker-compose.deploy.yml logs -f
docker compose --env-file .env.deploy -f docker-compose.deploy.yml down
```

停止服务不会删除数据库。如需彻底清空部署版数据，需在确认后额外执行带 `--volumes` 的关闭命令。
