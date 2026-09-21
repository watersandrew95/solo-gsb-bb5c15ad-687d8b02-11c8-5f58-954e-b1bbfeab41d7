# Webhook Ledger API

一个基于 **Python + FastAPI + SQLite** 的 webhook 账本服务：接收带 HMAC
签名的收入（income）/退款（refund）事件，按事件 ID 幂等入账，把事件折叠
（fold）成账户余额，支持乱序投递、余额查询与基于事件重放的对账。

## 特性

- **签名校验**：Stripe 风格的 `t=<ts>,v1=<hmac_sha256>` 签名头，常量时间
  比较，默认 5 分钟时间窗防重放。
- **幂等入账**：以 `event.id` 去重；也支持发送方幂等键
  `idempotency_key`（按账户作用域）。重复投递安全，金额只入账一次；同一
  幂等键对应不同事件 ID 返回 `409 Conflict`。
- **乱序余额折叠**：入账时把 income 记为 `+amount`、refund 记为
  `-amount`，余额是有符号金额的**纯求和**。事件无论按什么顺序到达，余额
  都收敛到同一结果。事件插入与余额更新在同一个 `BEGIN IMMEDIATE` 事务中
  完成，并发投递不会重复入账。
- **余额查询**：按账户返回各币种当前余额及版本号。
- **对账 / 事件重放**：给定 `[start, end)` 时间窗，从原始事件重新折叠出
  净额（replay），可与外部期望账本逐项比对返回差异；同时用**全量事件**
  重新折叠并与物化余额表交叉校验，发现存储漂移（integrity issue）。
- 金额使用**最小货币单位的整数**（如分），多币种、多账户隔离，SQLite WAL
  持久化到 Docker volume。

## 一条命令启动

```bash
docker compose up --build
```

服务监听 http://localhost:8000 ，交互式文档：

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

> 生产环境务必通过环境变量覆盖 `WEBHOOK_SECRET`（见下方配置）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/webhooks/events` | 接收签名事件，幂等入账 |
| `GET`  | `/accounts/{account_id}/balance` | 查询账户各币种当前余额 |
| `GET`  | `/accounts/{account_id}/events?limit=100` | 查询账户事件（调试用） |
| `POST` | `/reconcile` | 重放时间窗事件，返回差异与完整性问题 |
| `GET`  | `/health` | 健康检查 |

### 事件体

```json
{
  "id": "evt_1001",
  "account_id": "acct_1",
  "type": "income",
  "amount": 5000,
  "currency": "USD",
  "occurred_at": "2026-09-21T08:30:00Z",
  "idempotency_key": "order-42"
}
```

- `type`：`income` 或 `refund`
- `amount`：正整数，最小货币单位（5000 = 50.00 USD）
- `currency`：3 位 ISO-4217（大小写不敏感，存储时归一化为大写）
- `occurred_at`：ISO-8601 / RFC3339 时间（重放与排序的依据）
- `idempotency_key`：可选；缺省时用 `id` 去重

成功响应：`200 {"status":"received","event_id":"evt_1001","duplicate":false}`；
重复投递：`200 {"status":"duplicate",...,"duplicate":true}`（不重复入账）。

### 签名方案

请求头（二选一）：`Stripe-Signature` 或 `X-Signature`，格式：

```
t=1726907400,v1=<hex>
v1 = HMAC_SHA256(secret, f"{t}." + raw_body)
```

注意签名覆盖的是**原始字节**（服务端先验签再解析 JSON）。失败返回
`401`：缺签名头 / 签名不匹配 / 时间戳超出容忍窗。

Python 签名示例：

```python
import hmac, hashlib, time

def sign(secret: str, raw: bytes, ts: int | None = None) -> str:
    ts = int(time.time() if ts is None else ts)
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + raw,
                   hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"
```

### 余额

```bash
curl http://localhost:8000/accounts/acct_1/balance
# [{"account_id":"acct_1","currency":"USD","balance":3800,"version":2}]
```

### 对账 / 重放

```json
POST /reconcile
{
  "start": "2026-09-21T00:00:00Z",
  "end":   "2026-09-22T00:00:00Z",
  "expected": { "acct_1": { "USD": 3800 } }
}
```

- `start` / `end`：ISO-8601 字符串或 unix 秒；窗口半开 `[start, end)`；
  `end` 省略表示到当前；两者都省略时默认重放最近
  `REPLAY_WINDOW_SECONDS`（默认 86400 秒）。
- `expected`：可选的外部参考值。语义是**该时间窗内的净变动**（账户→币种
  →净额），与从事件重放折叠出的净额比对。
- `replayed_balances`：重放窗口折叠出的净额。
- `differences`：`expected` 与重放结果不一致的逐项差异
  （`replayed` / `expected` / `delta`）。
- `integrity_issues`：用全量事件重放与物化 `balances` 表交叉校验的结果，
  非空表示事件流与余额表之间存在漂移（`drift` 为差值）。

## 端到端示例

仓库自带仅依赖标准库的客户端 `examples/send_event.py`：

```bash
export WEBHOOK_SECRET=whsec_dev_secret_change_me   # 与 compose 一致
python examples/send_event.py income acct_1 5000 USD evt_1
python examples/send_event.py refund acct_1 1200 USD evt_2
python examples/send_event.py balance acct_1
python examples/send_event.py replay
```

## 本地开发（不用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
export LEDGER_DB_PATH=./data/ledger.db
uvicorn app.main:app --reload --port 8000
```

跑测试：

```bash
pip install pytest httpx
python -m pytest
```

测试覆盖：签名通过/缺失/错误/过期、事件 ID 幂等、幂等键冲突、乱序折叠、
多币种、多账户隔离、重放差异、余额漂移检测（共 14 项）。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WEBHOOK_SECRET` | `whsec_dev_secret_change_me` | HMAC 签名密钥，**生产必须覆盖** |
| `SIGNATURE_TOLERANCE_SECONDS` | `300` | 签名时间戳容忍秒数；`0` 关闭时间窗检查 |
| `REPLAY_WINDOW_SECONDS` | `86400` | `/reconcile` 未指定窗口时的默认回溯长度 |
| `LEDGER_DB_PATH` | `/data/ledger.db` | SQLite 数据库路径 |

## 数据模型

```text
events
  id (PK)  account_id  type  amount  currency
  signed_amount  occurred_at  received_at  idem_key  payload
  索引: account_id / occurred_at / (account_id, idem_key)

balances
  (account_id, currency) PK  balance  version
```

入账在单事务内执行：查重（`id`，再查账户作用域内 `idem_key`）→ 插入事件 →
`INSERT ... ON CONFLICT DO UPDATE SET balance = balance + ?` 折叠余额。

## 设计说明

- **为什么求和折叠天然支持乱序**：余额是事件的可交换、可结合聚合
  （Σ signed_amount），不依赖到达顺序或“上一余额”，迟到事件插入后余额
  立即一致；`occurred_at` 只用于重放窗口与展示排序。
- **幂等两层**：事件 ID 是全局主键（重投/at-least-once 安全）；幂等键覆盖
  “不同事件 ID 但同一笔业务”的场景，冲突显式报错而不是静默吞掉。
- **对账双校验**：窗口重放用于和外部账比对净额；全量重放对物化余额表做
  内部一致性检查，能发现任何非应用路径造成的存储偏差。
