# DNS SOA 序列号审计 API

基于 **Python + FastAPI + SQLite** 的 DNS SOA serial 审计服务：

- 客户端提交区域名与**按观测时间排列**的 32 位 serial 列表；
- 服务端按 **RFC 1982** 模运算逐项判定 `advance`（前进）/ `regression`（倒退）/ `equal`（相等）/ `incomparable`（不可比较），并标出**可能使从服务器拒绝更新的节点**（`slave_reject_risk`）；
- 提供 **YYYYMMDDnn** 风格的下一序列号建议，保证相对当前值为有效前进；当日编号耗尽或存在半区间歧义时返回 **409**；
- 审计记录持久化于 SQLite，可按 ID 查询；
- 支持基于已有审计**即时生成序列修复预演**（不落库）：首个 serial 不变，其后各项与上一个修复后值按 RFC 1982 比较，非有效前进即替换为上一修复后值加 1（32 位无符号回绕）。

## 快速启动（单条命令）

```bash
docker compose up --build
```

启动后访问 **http://localhost:8000**（交互式文档见 http://localhost:8000/docs）。

依赖安装与 SQLite 建表均在启动时自动完成，无需额外初始化步骤；数据通过命名卷 `dns-audit-data` 持久化。

不使用 Docker 时也可本地运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/audits` | 提交审计，返回逐项判定结果（201） |
| `GET` | `/api/audits/{id}` | 按 ID 查询审计记录（不存在返回 404） |
| `GET` | `/api/audits/{id}/repair-plan` | 基于已有审计生成序列修复预演，不落库（不存在返回 404） |
| `GET` | `/api/audits` | 审计记录列表（摘要，倒序） |
| `GET` | `/api/next-serial?current=<serial>` | YYYYMMDDnn 下一序列号建议（歧义/耗尽返回 409） |
| `GET` | `/health` | 健康检查 |

### 提交审计

```bash
curl -s -X POST http://localhost:8000/api/audits \
  -H 'Content-Type: application/json' \
  -d '{"zone": "example.com", "serials": [2026091800, 2026091801, 2026091801, 2026091705]}'
```

响应（节选）：

```json
{
  "id": 1,
  "zone": "example.com",
  "created_at": "2026-09-18T08:00:00.000000Z",
  "items": [
    {"index": 0, "serial": 2026091800, "relation": "initial",    "delta": null,      "slave_reject_risk": false, "note": "首个观测值，作为后续比较基准"},
    {"index": 1, "serial": 2026091801, "relation": "advance",    "delta": 1,         "slave_reject_risk": false, "note": "按 RFC 1982 为有效前进，从服务器可正常接受更新"},
    {"index": 2, "serial": 2026091801, "relation": "equal",      "delta": 0,         "slave_reject_risk": false, "note": "与上一观测值相等，从服务器不会触发区域传输"},
    {"index": 3, "serial": 2026091705, "relation": "regression", "delta": -96,       "slave_reject_risk": true,  "note": "序列号倒退，从服务器将认为本端数据更旧而拒绝更新"}
  ],
  "summary": {"total": 4, "advance": 1, "equal": 1, "regression": 1, "incomparable": 0, "risk_indexes": [3]}
}
```

### 按 ID 查询

```bash
curl -s http://localhost:8000/api/audits/1
```

### 序列修复预演

对已提交审计即时生成修复方案，**结果不写入数据库**，也不修改原审计记录。首个 serial 保持不变；其后每项都与**上一个修复后值**按 RFC 1982 比较——有效前进则保留（`keep`），相等/倒退/不可比较时替换（`replace`）为上一个修复后值加 1，并按 32 位无符号整数（模 `2^32`）回绕。

```bash
curl -s http://localhost:8000/api/audits/1/repair-plan
```

```json
{
  "id": 1,
  "zone": "example.com",
  "created_at": "2026-09-18T08:00:00.000000Z",
  "items": [
    {"index": 0, "original": 2026091800, "effective": 2026091800, "action": "keep",    "reason": "首个观测值保持不变，作为修复序列的比较基准"},
    {"index": 1, "original": 2026091801, "effective": 2026091801, "action": "keep",    "reason": "相对上一修复后值为 RFC 1982 有效前进，保留原值"},
    {"index": 2, "original": 2026091801, "effective": 2026091802, "action": "replace", "reason": "与上一修复后值相等，未形成有效前进，替换为上一修复后值加 1"},
    {"index": 3, "original": 2026091705, "effective": 2026091803, "action": "replace", "reason": "相对上一修复后值倒退，从服务器将拒绝更新，替换为上一修复后值加 1"}
  ],
  "summary": {"total": 4, "replaced": 2}
}
```

- 每项返回 `original`（原始值）、`effective`（修复后生效值）、`action`（`keep`/`replace`）与 `reason`（保留或替换原因），`summary.replaced` 汇总替换数量；
- 例如 `4294967295 → 4294967295` 会替换为 `0`（回绕后仍是有效前进）；审计 ID 不存在时返回 **404**。

### 下一序列号建议

```bash
curl -s 'http://localhost:8000/api/next-serial?current=2026091807'
# {"current":2026091807,"next":2026091808,"date":"20260918","note":"建议值相对当前值为 RFC 1982 意义下的有效前进"}
```

- 当前值落后于当日基准（含 32 位回绕场景）时，建议当日的 `YYYYMMDD00`；
- 当前值落在当日区间内时，建议 `current + 1`；
- `nn` 已达 `99`（当日编号耗尽）、当前值领先于当日区间、或与当日基准相距恰好 `2^31`（半区间歧义）时返回 **409**；
- 可选参数 `today=YYYY-MM-DD` 指定建议所基于的日期（缺省为服务器 UTC 当日）。

## 判定规则（RFC 1982）

serial 是模 `2^32` 环形空间上的值。设前向距离 `d = (current - previous) mod 2^32`：

| 关系 | 条件 | 从服务器行为 |
| --- | --- | --- |
| `advance` | `1 ≤ d ≤ 2^31 - 1` | 接受更新 |
| `equal` | `d = 0` | 不触发传输 |
| `regression` | `2^31 + 1 ≤ d ≤ 2^32 - 1` | **拒绝更新**（风险标记） |
| `incomparable` | `d = 2^31` | 行为不确定（风险标记） |

因此 `4294967295 → 0` 属于前进（`d = 1`），而 `0 → 2147483648` 不可比较。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DNS_AUDIT_DB` | `audits.db`（容器内 `/data/audits.db`） | SQLite 数据库文件路径 |

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

## 项目结构

```
app/
  main.py      # FastAPI 应用与路由（启动时初始化数据库）
  rfc1982.py   # RFC 1982 序列号算术
  suggest.py   # YYYYMMDDnn 下一序列号建议
  repair.py    # 序列修复预演（不落库）
  service.py   # 逐项判定与汇总
  db.py        # SQLite 持久层
  schemas.py   # 请求/响应模型与校验
tests/
  test_api.py  # API 与算术测试
```
