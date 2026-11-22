# 运营归因分析台

FastAPI + SQLite 的运营数据分析 Web 应用：接收同一用户的触点与转化事件，
在**归因窗口**与**每个触点的承接上限**约束下做归因匹配。

## 功能

- **按用户接收事件**：网页表单或 `POST /attribute` 提交 `touchpoints` / `conversions`。
- **合法触点**：只有发生在转化**之前**、且在归因窗口内的触点才可参与匹配。
- **最大化归因**：在触点容量约束下先最大化已归因转化数；在最大解集合中，
  按触点**优先级高 → 时间更新 → ID 升序**的偏好稳定决胜（最小代价最大流）。
- **可解释结果**：每个转化给出归因触点或未归因原因
  （`no_eligible_touchpoint` / `capacity_exhausted`）、窗口内候选触点、
  时间差；并提供触点余量与渠道汇总。
- **幂等写入**：事件按 `(user_id, event_id)` 去重；内容一致重复提交只计为
  忽略，内容冲突整批拒绝（409）。
- **整批校验**：任一字段非法（时间、类型、负数、未知字段、空批次等）则
  整批 422 拒绝，不写入任何数据。
- **页面查看**：单页应用可浏览历史用户、转化明细、匹配链时间轴、触点容量和渠道汇总。

## 启动

### Docker Compose

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在命名卷 `attribution_data` 中（容器内 `/data/attribution.db`），
`docker compose down` 不会删除数据；需要清空时执行 `docker compose down -v`。

默认端口 8000，可通过环境变量覆盖：

```bash
ATTRIBUTION_PORT=8080 docker compose up --build
```

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### 测试

```bash
pip install pytest httpx
python3 -m pytest
```

## API

### `POST /attribute`

```json
{
  "user_id": "u_1001",
  "attribution_window_seconds": 604800,
  "default_touch_capacity": 1,
  "touchpoints": [
    {"id": "tp1", "occurred_at": "2026-11-01T09:00:00Z", "channel": "搜索广告", "priority": 3, "capacity": 2},
    {"id": "tp2", "occurred_at": "2026-11-02T10:00:00Z", "channel": "社交媒体", "priority": 2}
  ],
  "conversions": [
    {"id": "cv1", "occurred_at": "2026-11-03T12:00:00Z", "value": 199}
  ]
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `user_id` | 非空字符串，事件集合按用户隔离 |
| `attribution_window_seconds` | 归因窗口（秒），正整数；合法区间为 `[转化时间 - 窗口, 转化时间)`，恰好等于窗口下界也算命中，与转化同一时刻的触点不算“此前” |
| `default_touch_capacity` | 未显式设置 `capacity` 的触点默认承接上限，非负整数 |
| `touchpoints[].id` | 触点在用户内唯一的事件 ID |
| `touchpoints[].occurred_at` | ISO 8601；无时区按 UTC 处理 |
| `touchpoints[].channel` | 渠道名，非空 |
| `touchpoints[].priority` | 整数，越大越优先，默认 0 |
| `touchpoints[].capacity` | 该触点最多承接的转化数，`null` 表示使用默认容量 |
| `conversions[].value` | 非负有限数字，默认 0 |

响应核心结构：

```json
{
  "user_id": "u_1001",
  "settings": {"attribution_window_seconds": 604800, "default_touch_capacity": 1},
  "totals": {"conversions": 6, "attributed_conversions": 4, "unattributed_conversions": 2, "attributed_value": 398.0},
  "unattributed_reasons": {"no_eligible_touchpoint": 1, "capacity_exhausted": 1},
  "conversions": [
    {
      "conversion_id": "cv1",
      "attributed": true,
      "touchpoint_id": "tp3",
      "channel": "邮件",
      "time_gap_seconds": 14400.0,
      "reason": null,
      "eligible_touchpoints": [{"touchpoint_id": "tp3", "selected": true, "...": "..."}]
    }
  ],
  "touchpoints": [{"touchpoint_id": "tp3", "capacity": 1, "used": 1, "remaining": 0}],
  "channels": [{"channel": "邮件", "attributed_conversions": 1, "attributed_value": 199.0}],
  "received": {"inserted": 4, "duplicates_ignored": 0}
}
```

重复提交同一批次：

- 内容完全一致：返回 200，`inserted=0`，`duplicates_ignored` 为重复条数；
- 同一 ID 对应不同内容：返回 409 `event_id_conflict`，整批不写入。

### 其他端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 单页 Web 界面 |
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/users` | 已保存用户列表 |
| `GET` | `/api/users/{user_id}/attribution` | 按已保存数据重新计算并返回归因结果 |
| `GET` | `/api/users/{user_id}/events` | 该用户已保存的原始事件 |
| `GET` | `/docs` | 自动生成的 OpenAPI 文档 |

### 错误格式

```json
{
  "error": {
    "code": "invalid_payload",
    "message": "请求校验失败，整批事件均未被处理。",
    "details": [{"field": "body.conversions.0.value", "message": "..."}]
  }
}
```

常见错误码：`invalid_payload`（422）、`duplicate_event_id`（422）、
`candidate_space_too_large`（422）、`payload_too_large`（413）、
`event_id_conflict`（409）、`user_not_found`（404）。

## 匹配算法

将问题建模为**带容量的二分匹配**：

1. 源点 → 每个转化（容量 1）；
2. 转化 → 合法触点（转化之前且窗口内，容量 1，代价为该触点的偏好名次）；
3. 触点 → 汇点（容量为触点的承接上限，代价 0）。

求解**最小费用最大流**（逐次最短增广路 + Johnson 势函数）。最大流值即最大化
已归因转化数；在最大解中最小化总偏好名次，等价于优先使用高优先级、更新、
ID 更小的触点。算法结果完全确定，同一输入重复计算得到同一匹配。

为保护服务，单次请求的候选连接数超过 120,000 时返回 422，建议缩小窗口或拆分
事件批次。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ATTRIBUTION_DB_PATH` | `data/attribution.db` | SQLite 文件路径 |
| `ATTRIBUTION_MAX_BODY_BYTES` | `16777216` | 请求体上限（字节） |

## 并发与持久化

- SQLite 开启 WAL；写入使用 `BEGIN IMMEDIATE` 事务，幂等检查与插入原子完成。
- 事件、设置与幂等哈希在同一个事务中落库；校验或冲突失败时整批回滚。
- `user_summaries` 表随成功提交的写入维护每个用户的触点数、成交数和最近更新
  时间，与事件明细在**同一事务**内更新：只有首次写入的事件会增加计数，设置
  更新、重复提交、冲突拒绝和回滚都不改变计数。`GET /api/users` 直接读取摘要，
  不再随请求聚合 `events`，返回字段与排序保持不变。
- 服务启动时以 `events` 明细为准全量重建摘要（幂等），因此旧版本数据库会自动
  补齐，进程重启或意外偏差后摘要仍与明细一致。
