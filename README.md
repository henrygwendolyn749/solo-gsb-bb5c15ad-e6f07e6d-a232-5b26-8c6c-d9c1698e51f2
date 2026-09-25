# 越野赛分段计时 API

基于 FastAPI + SQLite 的越野赛分段计时服务：

- 接收带**唯一事件号**、选手号、站点序号、发生时间的打卡；
- 支持**批量登记**（1~100 条，同一事务，批内可更正前面刚写入的记录，任一失败整体回滚）；
- 支持按事件号**更正**（新事件取代原事件）与**撤销**；
- 支持**更正预演**：试算更正前后结果，整体回滚，不产生任何副作用；
- 同一站点多条有效打卡取**最早时间**；
- 按站点顺序重算相邻站分段用时，**乱序补传**可填补缺段，更正 / 撤销即时更新结果；
- 相邻站缺打卡或**时间倒退**时，该分段标记为 `pending`（待定）并说明原因；
- 提供选手分段结果查询；
- 支持**成绩发布**：在同一事务中读取当前全部打卡，整版快照原子保存为递增版本
  （并发打卡 / 更正 / 撤销在发布事务期间排队，同一版本不会混入不同时间点的数据）；
  已发布版本永久冻结，后续打卡、更正或撤销只影响实时成绩。

## 启动

```bash
docker compose up --build
```

一条命令即可：容器启动时自动建表，数据库为空时写入示例数据（选手 R001–R006，覆盖完整成绩、乱序补传、缺站、更正、撤销、时间倒退等场景）。

- 本地端口：**8000**
- API 地址：http://localhost:8000
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/health

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STATION_COUNT` | `6` | 赛道站点总数，分段为 1→2 … (N-1)→N |
| `DB_PATH` | 容器内 `/data/timing.db`，本地 `timing.db` | SQLite 数据库文件路径 |

Compose 通过命名卷 `timing-data` 持久化数据；删除卷（`docker compose down -v`）后重启会重新初始化并播种示例数据。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/checkins` | 登记打卡；携带 `corrects` 字段即为更正指定事件 |
| `POST` | `/api/checkins/batch` | 批量登记 1~100 条打卡：同一事务处理，任一失败整体回滚 |
| `POST` | `/api/checkins/preview-correction` | 预演更正：返回更正前后结果对照，整体回滚、无副作用 |
| `POST` | `/api/checkins/{event_no}/revoke` | 按事件号撤销打卡 |
| `GET` | `/api/checkins?runner_id=` | 打卡列表（含已更正 / 已撤销记录） |
| `GET` | `/api/checkins/{event_no}` | 按事件号查询单条打卡 |
| `GET` | `/api/runners/{runner_id}/splits` | 选手分段结果 |
| `POST` | `/api/publications` | 发布当前实时成绩为新版本（整版快照，版本号递增） |
| `GET` | `/api/publications/latest` | 查询最新发布版本；尚未发布时返回 `404` |
| `GET` | `/api/publications/{version}` | 查询指定版本的完整成绩；版本不存在返回 `404` |

### 示例

登记打卡：

```bash
curl -X POST http://localhost:8000/api/checkins \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"E1001","runner_id":"R100","station_seq":1,"occurred_at":"2026-09-25T08:00:00"}'
```

更正（`E1002` 取代 `E1001`）：

```bash
curl -X POST http://localhost:8000/api/checkins \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"E1002","runner_id":"R100","station_seq":1,"occurred_at":"2026-09-25T08:05:00","corrects":"E1001"}'
```

批量登记（1~100 条，按顺序在同一事务内处理）：

```bash
curl -X POST http://localhost:8000/api/checkins/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "checkins": [
      {"event_no":"E2001","runner_id":"R100","station_seq":1,"occurred_at":"2026-09-25T08:00:00"},
      {"event_no":"E2002X","runner_id":"R100","station_seq":2,"occurred_at":"2026-09-25T12:00:00"},
      {"event_no":"E2002","runner_id":"R100","station_seq":2,"occurred_at":"2026-09-25T08:32:00","corrects":"E2002X"},
      {"event_no":"E2003","runner_id":"R100","station_seq":3,"occurred_at":"2026-09-25T09:12:00"}
    ]
  }'
```

每条沿用单条登记的字段与校验；批内后续记录可用 `corrects` 更正**本批前面刚写入且仍有效**的事件
（上例中 `E2002` 更正同批的 `E2002X`）。全部成功时返回 `201`，携带各条事件号（按提交顺序）以及
受影响选手按现有分段规则计算的最终完成状态与总用时：

```json
{
  "results": [
    {"index": 0, "event_no": "E2001"},
    {"index": 1, "event_no": "E2002X"},
    {"index": 2, "event_no": "E2002"},
    {"index": 3, "event_no": "E2003"}
  ],
  "affected_runners": [
    {"runner_id": "R100", "complete": false, "total_seconds": null, "total": null}
  ]
}
```

任一条失败时整批回滚（新增记录与状态变更全部撤销），响应为对应语义的错误码（404 / 409 / 422），
并在 `detail.failures` 中给出失败下标（从 0 开始）与原因。前向引用（`corrects` 指向本批后面才出现
的事件，含更正自身）、批内重复事件号、无效更正（目标不存在或已失效）均按此返回：

```json
{
  "detail": {
    "message": "批量打卡在下标 2 处失败，整批已回滚",
    "failures": [
      {"index": 2, "event_no": "E2003", "reason": "事件号 E2003 与本批下标 0 重复"}
    ]
  }
}
```

预演更正（字段与正式更正相同，`corrects` 必填且须指向有效事件）：

```bash
curl -X POST http://localhost:8000/api/checkins/preview-correction \
  -H 'Content-Type: application/json' \
  -d '{"event_no":"E1002","runner_id":"R100","station_seq":1,"occurred_at":"2026-09-25T08:05:00","corrects":"E1001"}'
```

预演与正式更正使用相同的事件号唯一性、站点范围与状态校验；按现有"同站取最早 + 相邻站"规则
试算更正前后结果，返回受影响选手各自的完成状态、总用时及**仅发生变化的分段**；更正跨选手时
会同时覆盖原选手与新选手。整个预演在事务内试算后**整体回滚**：不占用事件号、不改变打卡状态
或成绩，相同请求随后可原样正式提交。响应节选：

```json
{
  "event_no": "E1002",
  "corrects": "E1001",
  "affected_runners": [
    {
      "runner_id": "R100",
      "before": {"complete": false, "total_seconds": null, "total": null},
      "after": {"complete": true, "total_seconds": 12600, "total": "3:30:00"},
      "changed_segments": [
        {
          "from_station": 1,
          "to_station": 2,
          "before": {"from_station": 1, "to_station": 2, "status": "pending", "reason": "站点 1 缺少有效打卡"},
          "after": {"from_station": 1, "to_station": 2, "status": "ok", "duration_seconds": 1920, "duration": "0:32:00"}
        }
      ]
    }
  ]
}
```

撤销：

```bash
curl -X POST http://localhost:8000/api/checkins/E1002/revoke
```

查询分段结果：

```bash
curl http://localhost:8000/api/runners/R001/splits
```

响应节选：

```json
{
  "runner_id": "R001",
  "complete": true,
  "total_seconds": 12600,
  "total": "3:30:00",
  "segments": [
    {"from_station": 1, "to_station": 2, "status": "ok", "duration_seconds": 1920, "duration": "0:32:00"},
    {"from_station": 3, "to_station": 4, "status": "pending", "reason": "站点 4 缺少有效打卡"}
  ]
}
```

## 成绩发布

发布动作把**当前实时成绩整版固化**为一个不可变版本，版本号从 1 开始递增。发布在同一个 SQLite
事务中读取当前全部打卡，并按现有分段规则（同站取最早、相邻站计算、缺站 / 时间倒退为 `pending`）
为**所有有打卡记录的选手**（含仅有已撤销记录的选手）生成结果——未完赛选手同样保留其 `pending`
分段。事务以 `BEGIN IMMEDIATE` 立即取得库级写锁，期间的打卡 / 更正 / 撤销只能排队，因此**同一
版本不会混入不同时间点的数据**；版本头与全部选手快照在该事务内原子落盘。

发布当前成绩（`201 Created`，返回该版本完整成绩）：

```bash
curl -X POST http://localhost:8000/api/publications
```

```json
{
  "version": 1,
  "created_at": "2026-09-25T03:30:00.123456+00:00",
  "runner_count": 6,
  "complete_count": 3,
  "station_count": 6,
  "runners": [
    {
      "runner_id": "R001",
      "complete": true,
      "total_seconds": 12600,
      "total": "3:30:00",
      "stations": [
        {"station_seq": 1, "event_no": "S-R001-1", "occurred_at": "2026-09-25T08:00:00+00:00"}
      ],
      "segments": [
        {"from_station": 1, "to_station": 2, "status": "ok", "duration_seconds": 1920, "duration": "0:32:00", "reason": null}
      ]
    },
    {
      "runner_id": "R003",
      "complete": false,
      "total_seconds": null,
      "total": null,
      "stations": [
        {"station_seq": 1, "event_no": "S-R003-1", "occurred_at": "2026-09-25T08:10:00+00:00"}
      ],
      "segments": [
        {"from_station": 3, "to_station": 4, "status": "pending", "duration_seconds": null, "duration": null, "reason": "站点 4 缺少有效打卡"}
      ]
    }
  ]
}
```

查询指定版本与最新版本：

```bash
curl http://localhost:8000/api/publications/1
curl http://localhost:8000/api/publications/latest
```

- 尚未发布任何版本时，`GET /api/publications/latest` 返回 `404`；
- 查询不存在的版本号（如 `/api/publications/99`）返回 `404`。

再次调用 `POST /api/publications` 生成下一版（版本号递增）。**后续打卡、更正或撤销只影响实时
成绩，不改已发布版本**：例如发布 v1 后补传 / 撤销了打卡，v1 的内容保持不变；再次发布得到的 v2
才反映这些变化。

```bash
# 发布后撤销 R001 第 3 站：实时成绩变为未完赛，但 v1 中 R001 仍为完赛
curl -X POST http://localhost:8000/api/checkins/S-R001-3/revoke
curl http://localhost:8000/api/publications/1     # R001 仍 complete=true（冻结）
curl http://localhost:8000/api/runners/R001/splits  # 实时结果已为 complete=false
curl -X POST http://localhost:8000/api/publications  # 产生 v2，反映撤销后的状态
curl http://localhost:8000/api/publications/latest   # 现在返回 v2
```

## 本地开发

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # 默认监听 8000 端口
pytest                               # 运行测试
```

## 实现说明

- 打卡记录永久保留，更正 / 撤销只改变状态（`active` / `superseded` / `revoked`），便于审计；
- 分段结果在查询时基于全部有效打卡实时重算，因此补传、更正、撤销立即反映到结果中；
- 时间统一归一化为 UTC 存储，无时区的时间按 UTC 处理；
- 更正与写入在同一事务中完成，事件号唯一性由数据库主键保证（重复返回 409）；
- 批量登记在同一数据库事务内按顺序处理全部记录，任一失败即整体回滚（含已做的状态变更），
  成功时基于全部新数据按现有分段规则重算受影响选手的最终完成状态与总用时；
- 更正预演复用正式更正的校验与写入逻辑，在 `SAVEPOINT` 内试算后 `ROLLBACK TO` 整体回滚，对数据库零副作用；
- 成绩发布以单条 `BEGIN IMMEDIATE` 事务读取当前全部打卡并按现有规则重算所有有记录选手
  （未完赛选手保留 `pending` 分段），版本头（自增版本号）与每位选手的 JSON 快照在同一事务内
  原子写入 `publications` / `publication_runners` 两表；写锁使并发打卡 / 更正 / 撤销排队，
  保证同一版本数据来自同一提交点；已发布快照不可变，后续写入只改变实时重算结果；
- 所有数据库连接设置 `busy_timeout=5000`：并发写相互排队而不是立即返回 `SQLITE_BUSY`。
