# 网页端架构

```text
浏览器 ──HTTPS──> Next.js 前端
                    │
                    └──HTTPS/JSON──> FastAPI
                                       ├── PostgreSQL（持久数据）
                                       └── Redis
                                           ├── 登录与节点流量计数
                                           └── Stream 消息队列 ──> Worker ──> PostgreSQL

桌面 EXE ──HTTPS/JSON + X-Node-Id───────────────────────────┘
```

## 请求处理

1. 桌面端先创建一个自增批次编号。
2. 每次最多上传 50 条检测结果。
3. FastAPI 使用 Redis 对 `X-Node-Id` 进行短时间窗口计数。
4. 请求量在阈值内时直接写入 PostgreSQL。
5. 超过阈值后，结果先用 Fernet 加密，再写入 Redis Stream，并立即返回 `202 Accepted`。
6. Worker 使用 Redis Consumer Group 消费，失败消息不确认并在空闲 30 秒后重新认领。
7. PostgreSQL 使用 `(batch_id, item_index)` 唯一约束保证重复投递不会生成重复结果。

## 数据安全

- 登录密码使用 Argon2id 哈希。
- 登录会话只在 HttpOnly Cookie 中保存，数据库只保存带服务端密钥的 Token 哈希。
- API 密钥使用 Fernet 加密后存入 PostgreSQL。
- 进入 Redis 队列的任务也会加密，Redis 中不保存可直接读取的明文密钥。
- 本地真实密码只放在 `web/.env`，该文件被忽略；示例配置不包含真实密码。
