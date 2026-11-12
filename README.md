# 联合目录补采采集服务

联合目录管理员对单馆书目进行补采的 OAI-PMH 采集服务。核心保证：**翻页失败绝不让半批记录入库**，
以及**数据卷与 OAI 源地址绑定，防止重部署时误指演练源覆盖正式库书目**。

## 需求与实现对照

| 需求 | 实现 |
| --- | --- |
| FastAPI 接收截止日，首次另填起始日 | `POST /api/v1/harvest`，请求体 `until` 必填、`from` 选填 |
| 之后从上次成功采集的截止日接着采，起止日都包含 | `from` 留空时取 `harvest_progress.last_until` 作为起始日（含当天） |
| OAI-PMH ListRecords 按日期拉取 oai_dc | 首请求 `verb=ListRecords&metadataPrefix=oai_dc&from=...&until=...` |
| 续页用 resumptionToken 替代日期和格式参数 | 续页请求只带 `verb` + `resumptionToken`；样例源对混用参数返回 `badArgument` |
| 令牌失效丢弃已取页，按原区间重抓一次 | 捕获 `badResumptionToken` 后清空已取页，用原始 from/until 重新拉取；只重抓一次 |
| 按标识更新 SQLite 书目 | `bib_records.identifier` 主键 upsert，重复采集幂等 |
| 遇删除标记就撤下 | header `status=deleted` 的记录置为 `withdrawn`（库中不存在则写墓碑） |
| 整批采完才提交书目变更和截止日 | 单事务 `BEGIN IMMEDIATE`，全部页取完才写库并提交；异常整体回滚 |
| 无匹配也推进截止日 | `noRecordsMatch` 不写书目，同事务推进 `last_until` 并绑定源地址 |
| **新库首次成功即绑定源地址（同事务提交，失败不绑定）** | `source_binding` 单行表；`bind_source` 与书目 upsert、进度推进在同一事务 |
| **旧库未绑定须经单独配置项核实，地址相同才可补绑定** | 环境变量 `CONFIRMED_OAI_ENDPOINT`；与当前 `OAI_ENDPOINT` 完全相同才放行 |
| **来源未确认/地址不符返回 409，不访问源、不写运行历史** | `binding.preflight` 在创建 OAI 客户端之前拦截；`harvest_runs` 不新增记录 |
| **原目录与进度仍可查询，改回原地址照常续采** | 409 不触碰任何数据；`/catalog`、`/progress`、`/source-binding` 正常可读 |
| Docker Compose 启动 API 和两个本地 OAI-PMH 样例源 | `docker compose up --build`：`harvester-api` + `oai-source`（正式）+ `oai-source-drill`（演练） |
| Swagger 发起采集并查询目录和进度 | http://localhost:8000/docs |

## 源地址绑定保护（防止演练源覆盖正式库）

**风险场景**：重部署补采服务时，若把 `OAI_ENDPOINT` 误指向演练源却沿用正式库的数据卷，
服务会拿正式库的截止日向演练源续采，并按相同 OAI 标识 upsert，直接覆盖正式库书目。

准入规则（在访问任何 OAI 源之前执行）：

1. **新库**（无书目、无续采进度）：首次补采**成功**时，把源地址与书目变更、续采进度
   在**同一个 SQLite 事务**内提交到 `source_binding`；无匹配也算成功、同样绑定。
   采集失败（源不可达、翻页失败等）整体回滚，**不产生绑定**。
2. **旧库已有书目或续采进度但未绑定**：来源无法自动确认，默认返回 **409**。
   管理员须通过**单独的配置项** `CONFIRMED_OAI_ENDPOINT` 填写核实过的原地址；
   只有它与当前 `OAI_ENDPOINT` **完全相同**才放行，并在本次成功采集的事务内补绑定。
3. **已绑定但当前地址与绑定地址不符**：返回 **409** 并说明当前地址与已绑定地址。

所有 409 都**不访问源、不写 `harvest_runs` 运行历史**；原目录和续采进度仍可通过
`/api/v1/catalog`、`/api/v1/progress` 查询，把 `OAI_ENDPOINT` 改回已绑定地址后即可照常续采。
绑定状态可随时通过 `GET /api/v1/source-binding` 查看（仅末尾斜杠差异会被归一化视为相同）。

## 目录结构

```
app/                  FastAPI 采集服务
  main.py             路由（采集/目录/进度/绑定状态/运行历史/健康检查）
  binding.py          源地址绑定准入检查（409 拦截，不访问源、不写历史）
  harvester.py        采集编排：翻页、令牌失效重抓、整批事务（含源地址绑定）
  oai_client.py       OAI-PMH 2.0 客户端（httpx + xml.etree）
  db.py               SQLite 表结构与事务操作（含 source_binding）
  schemas.py          Pydantic 请求/响应模型
  Dockerfile
sample-oai/           本地分页样例 OAI-PMH 源（纯标准库，同一镜像起正式/演练两个容器）
  server.py           27 条样例记录（rec-013 为删除记录），SOURCE_NAME 区分两个源
  Dockerfile
tests/                pytest 端到端测试
scripts/              辅助脚本（故障回滚演示）
docker-compose.yml
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

启动三个容器：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| `harvester-api` | http://localhost:8000/docs | 采集 API / Swagger，SQLite 存于命名卷 `harvester-data`（容器内 `/data/harvester.db`） |
| `oai-source` | http://localhost:8080/oai | 正式样例源（每页 10 条） |
| `oai-source-drill` | http://localhost:8081/oai | 演练源（每页 5 条，**相同 OAI 标识空间**，专用于误指演练） |

## 操作流程（Swagger 或 curl）

### 1. 首次采集：另填起始日，起止日都包含

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-15"}'
```

响应：

```json
{
  "status": "success",
  "from_date": "2026-10-01",
  "until_date": "2026-10-15",
  "pages": 2, "fetched": 15,
  "inserted": 14, "updated": 0, "withdrawn": 1,
  "retries": 0, "run_id": 1,
  "source_endpoint": "http://oai-source:8080/oai"
}
```

### 2. 后续采集：只填截止日，自动从上次截止日（含）续采

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"until": "2026-10-27"}'
# 实际区间：2026-10-15 ~ 2026-10-27（last_until 含当天，避免边界漏采）
```

### 3. 查询目录、进度与绑定状态

```bash
curl 'http://localhost:8000/api/v1/catalog?status=active'   # 在架（默认）
curl 'http://localhost:8000/api/v1/catalog?status=all'      # 含已撤下
curl 'http://localhost:8000/api/v1/catalog/oai:sample.lib:rec-013'
curl 'http://localhost:8000/api/v1/progress'                # 上次成功截止日
curl 'http://localhost:8000/api/v1/source-binding'          # 源地址绑定状态
curl 'http://localhost:8000/api/v1/runs'                    # 采集运行历史
```

### 4. 无匹配也推进截止日并完成绑定

对没有任何记录的区间发起采集，返回 `fetched=0` 且 `last_until` 照常推进；
新库的首次无匹配采集同样会绑定当前源地址。

## 重部署演练：保留数据卷换地址（Swagger 可复现）

1. 按上面的流程对正式源完成至少一次采集，数据卷 `harvester-data` 中已绑定
   `http://oai-source:8080/oai`。
2. 把 API 的 `OAI_ENDPOINT` 改成演练源（如 `http://oai-source-drill:8080/oai`）后重新部署，
   **数据卷保持挂载不变**。
3. 再调用 `POST /api/v1/harvest`，得到 **409**，响应说明当前地址与已绑定地址不符；
   此时演练源**不会收到任何请求**，`/api/v1/runs` 不新增记录，目录与进度原样可查。
4. 把 `OAI_ENDPOINT` 改回 `http://oai-source:8080/oai` 重新部署，续采立即恢复正常。

### 旧库未绑定时的核实流程

对于升级前已存在、带书目或进度但没有绑定记录的数据卷：

1. 直接采集会收到 409，提示来源未确认。
2. 管理员核实原地址后，在**单独配置项**中设置
   `CONFIRMED_OAI_ENDPOINT=http://oai-source:8080/oai`（与当前 `OAI_ENDPOINT` 相同）。
3. 重新发起采集即成功并在同事务内补绑定；若两者不符仍是 409。

## 令牌失效重抓的验证方法

样例源提供故障注入端点：

```bash
# 令下一次 resumptionToken 请求返回 badResumptionToken
curl -X POST http://localhost:8080/admin/fail-next
curl http://localhost:8080/admin/state

# 全区间 27 条共 3 页：第 1 页取到后续页令牌失效 -> 丢弃该页 -> 原区间重抓成功
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-27"}'
# 响应中 retries=1；运行历史与服务日志可查
curl -X POST http://localhost:8080/admin/reset   # 清除故障开关
```

若重抓过程中令牌**再次**失效（不可恢复），接口返回 `502`，本批所有书目变更、
截止日推进与源地址绑定全部回滚，`harvest_runs` 中记录一条 `failed` 历史——任何半批数据都不会进库。

## 事务与一致性说明

- 采集过程先在内存中收集全部页面，**不向 SQLite 写任何数据**；
- 全部页成功后，在单个 `BEGIN IMMEDIATE` 事务内完成 upsert、撤下、截止日推进、
  源地址绑定、运行历史更新；
- 事务中任何一步失败都会 rollback，库状态回到采集前（新库首采失败即不绑定）；
- 进程内互斥锁保证同一时刻只有一个采集任务，并发请求返回 `409`；
- 单进程部署不变，翻页规则不变（续页只用 resumptionToken，失效按原区间重抓一次）。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 终端 1：正式样例 OAI 源（端口 8080）
python3 sample-oai/server.py

# 终端 2：演练源（端口 8081，同一套标识）
PORT=8081 PAGE_SIZE=5 BASE_URL=http://127.0.0.1:8081/oai \
  SOURCE_NAME="演练 OAI-PMH 源" python3 sample-oai/server.py

# 终端 3：API
APP_DB_PATH=./data/harvester.db OAI_ENDPOINT=http://127.0.0.1:8080/oai \
  uvicorn app.main:app --reload

# 测试
pytest
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OAI_ENDPOINT` | `http://oai-source:8080/oai` | 当前使用的 OAI-PMH 端点 |
| `CONFIRMED_OAI_ENDPOINT` | 无 | **单独的核实配置项**：仅旧库未绑定时使用，须与 `OAI_ENDPOINT` 完全相同才允许补绑定 |
| `APP_DB_PATH` | `/data/harvester.db` | SQLite 数据库路径 |
| `PAGE_SIZE`（样例源） | `10` | 样例源每页记录数 |
| `TOKEN_TTL`（样例源） | `3600` | 样例源 resumptionToken 有效期秒数 |
| `SOURCE_NAME`（样例源） | `本地分页样例 OAI-PMH 源` | Identify 中的源名称，区分正式/演练源 |
