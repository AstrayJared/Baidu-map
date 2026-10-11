# 15 分钟生活圈双算法后端

后端同时提供两种 900 秒生活圈算法：百度边界搜索（E8.2，`local-multicross-e82`）使用真实步行端点证据做径向搜索与局部多边界重建；Hybrid v1.5 以 OSM 路网提供参考、百度详细步行路线核验。两者并存以供用户选择和开发对比，界面不标注速度或精度优劣。

## 启动

Python 3.11+，在backend目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt -e ../life-circle-algorithm
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

首次配置参考 `.env.example`，保留本地服务端BAIDU_MAP_AK、ANALYSIS_QPS和OSM数据路径。不要将服务端密钥放入前端。系统环境优先于.env。已有.env无需覆盖。

OSM图缓存、版本、覆盖边界、障碍和风险层见 [数据说明](../data/osm/README.md)。启动和/health不加载城市图，第一次Hybrid任务才惰性加载；缺失数据明确显示degraded。

## 接口

两个任务入口相互独立：

- `POST /api/analyses`：百度边界搜索（E8.2），使用原 `center` / `coordinateSystem` / `budget` / `clientRequestId` 请求。默认预算 400，保留 200/800 档；结果 `algorithm=local-multicross-e82`，只提供真实计算的 15 分钟圈，未知或未收敛结果保持部分结果语义。请求通过薄适配层进入团队 E8.2 核心，不经过旧自适应网格实现。
- `POST /api/v1/analysis/hybrid`：OSM＋百度算法，使用 HybridRequest：

```json
{"origin":{"lng":121.513925,"lat":31.313079},"coordinate_system":"bd09ll","config":{"max_baidu_requests":400},"client_request_id":"example-unique-id"}
```

两个前缀都支持任务查询、结果和取消；Hybrid 还支持按请求 ID 查询恢复。请求契约不可混用，服务端不会静默改用另一算法。Hybrid 结果额外提供只读的 `displayGeometry`：扣除 OSM 水体前的圈面外壳，仅用于地图外轮廓展示（不填色、不画内孔），计算几何、面积统计与诊断仍以 `geometry` 等原字段为准；旧响应缺少该字段时前端退回原几何外环显示。

E8.3（POI 引导联合构圈）仍为离线实验，不接入浏览器或生产 HTTP 入口，代码保留在 `backend/tools/endpoint_e83_*`。旧自适应网格实现保留作历史基线。

独立 `/api/v1/analysis/osm_offline` 继续作为离线基线接口。`ANALYSIS_PROVIDER=baidu` 控制纯百度任务的 Provider；`synthetic` 仅用于离线测试。Hybrid 始终从自己的入口运行。

每个管理器限制本算法的并发任务，并共享百度 QPS 限流器。Hybrid 账本位于 `.hybrid-ledgers`；重启不自动续跑。任务 completed 不等于精度验收通过；Hybrid 的 `facilitiesStatus=not_integrated` 表示设施没有接入。

## 设施检索的额度与缓存口径

四件事不要混为一谈，它们各自的生效值都能从 `/api/v2/capabilities` 读到：

| 限制 | 含义 | 在哪里看 |
| --- | --- | --- |
| QPS / 并发 | 每秒能发几次、同时几条在飞 | `quota.services.place.qps`、`maxInflight` |
| 单任务预算 | 一个任务最多花几次地点检索 | `budgets.poiRequests`（默认 60、上限 160） |
| 应用日预算 | 本应用一天最多花几次，按北京时间跨日，重启不清零 | `quota.services.place.dailyBudget` / `remainingToday` |
| 百度账号真实配额 | 不在本应用账本里 | 需按控制台授权自行核实 |

切换时间之后生效的是 fallback 档：此时只提高 `BAIDU_PLACE_DAILY_BUDGET` 不会有任何效果，要调的是 `BAIDU_FALLBACK_PLACE_DAILY_BUDGET`（`quota.tier` 报出当前生效档位）。

设施检索把两把尺子分开记：**网络额度**（真正派发出去的新调用，由任务桶与应用日账本计量，经 `ServicePool.attempt()` 在派发前原子扣减）与**本地处理上限**（页面调度步数，`poiPlanning.processingStepLimit`，缓存重放与细分也算）。所以：

- 命中缓存不消耗网络额度，缓存重放也就不会把一次检索提前停在上一次的边界上；
- 额度用完时仍继续读得到缓存，缺页逐页记下来因，结果按 partial 报出，不会把已取到的证据一起丢掉；
- 预检（补查与设施阶段共用）先按真实页键算出首轮页数、缓存可复用页数与预计新增调用数，再决定够不够：完全可复用的补查在日余额为 0 时也能提交。`initialPlan` 是**执行前**（派发第一页之前）算出的估算，不是提交时的额度预留，也不承诺翻页与细分跑在其中；最终仍由服务池在派发前裁定。

补查（`/api/v2/checkups/{taskId}/facility-extensions`）先认请求标识再算额度：同一个标识回来就取回原来那一次补查，不重新排队、不发请求、不扣额度；参数不同则返回 409，且不会被"余额不足"掩盖。它复用原体检冻结的圈面与同一个任务的页面缓存，只查本次选择的类别，不改原任务的修订、报告与评分。

页面缓存是进程内存：重启即失效，也不跨进程共享。因此本轮不支持重启后的断点续查，`partial` 也是终态、不会自动跨日恢复。

**同一个位置再来一次有两件事，别混**：原任务的**补查**（新补查标识）复用该任务的圈面与缓存，网络额度只花在缺页上，是"把额度用于新增检索"的那条路径；**重新体检**是新任务，默认不跨任务复用页面（未配 `CACHE_FRESHNESS_SECONDS`），所以它会重新花一次额度 —— 这不是缓存失效，而是数据使用上的取舍。两种做法都要看设施侧的结论：任务 `completed` **不代表**设施查完，查完与否读 `facilitiesStatus`（结果文件里的 `facilities.queryStatus` 与 `stopReason`，或 `statistics` 的页面/调用数）。

## 检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m tools.export_contract
```

以上不调用真实百度。两套算法的真实验收必须分别记录入口、配置与调用预算，不能用一套结果替代另一套。

[当前状态](../CURRENT_STATE.md) · [Hybrid设计](../HYBRID_ISOCHRONE_DESIGN.md) · [2.1失败报告](reports/baidu-v21-live-20260917-network/report.md)


### 设施检索预算（2026-10-10）

v2 体检首轮及手动补全轮默认、最大均为 1200 次地点检索尝试；队列提前完成即停止，失败与重试计入预算，缓存不占网络额度。路线核验总上限仍为 120。能力接口 `/api/v2/capabilities` 返回新预算，当前轮 `completion.roundPoiLimit` 返回已入库的实际额度；历史轮次与冻结报告保留原预算。检索仍受共享限流、配置的日额度和任务超时约束，不保证目录全量或所有区域均可判定。
