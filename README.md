# 网页端

网页端采用前后端分离架构：

- `frontend/`：Next.js 16 / React 前端，默认端口 `3000`。
- `backend/`：FastAPI 后端，默认端口 `8000`。
- PostgreSQL：持久化账号、会话、密钥及检测批次。
- Redis：登录限流、节点突发流量计数和检测结果消息队列。
- `worker`：从 Redis Stream 消费高峰期积压的检测结果并写入 PostgreSQL。

## 本地启动

1. 安装 Docker Desktop。
2. 确认 `web/.env` 只保存在本机，不提交源码。
3. 在本目录执行 `docker compose up --build`。
4. 打开 `http://localhost:3000`，后端健康检查为 `http://localhost:8000/health`。

数据库结构由 Alembic 管理。FastAPI 容器启动时会先执行最新迁移。桌面端可通过环境变量
`KEY_CONTROL_API_URL=http://localhost:8000` 上传到本地后端。

## 突发流量处理

每个桌面节点在短时间内的请求低于阈值时同步写入 PostgreSQL；超过阈值后，请求内容会先加密写入
Redis Stream，由独立 Worker 平滑消费。批次明细以 `(batch_id, item_index)` 保证幂等。
