# 15 分钟生活圈双算法后端

后端同时提供两种 900 秒生活圈算法：纯百度自适应网格方案速度较快；Hybrid v1.5 以 OSM 路网提供参考、百度详细步行路线核验，耗时更长但用于追求更准确的边界。两者必须并存以供用户选择和开发对比。

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

- `POST /api/analyses`：纯百度算法，使用原 `center` / `coordinateSystem` / `budget` / `clientRequestId` 请求。
- `POST /api/v1/analysis/hybrid`：OSM＋百度算法，使用 HybridRequest：

```json
{"origin":{"lng":121.513925,"lat":31.313079},"coordinate_system":"bd09ll","config":{"max_baidu_requests":400},"client_request_id":"example-unique-id"}
```

两个前缀都支持任务查询、结果和取消；Hybrid 还支持按请求 ID 查询恢复。请求契约不可混用，服务端不会静默改用另一算法。

独立 `/api/v1/analysis/osm_offline` 继续作为离线基线接口。`ANALYSIS_PROVIDER=baidu` 控制纯百度任务的 Provider；`synthetic` 仅用于离线测试。Hybrid 始终从自己的入口运行。

每个管理器限制本算法的并发任务，并共享百度 QPS 限流器。Hybrid 账本位于 `.hybrid-ledgers`；重启不自动续跑。任务 completed 不等于精度验收通过；Hybrid 的 `facilitiesStatus=not_integrated` 表示设施没有接入。

## 检查

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m tools.export_contract
```

以上不调用真实百度。两套算法的真实验收必须分别记录入口、配置与调用预算，不能用一套结果替代另一套。

[当前状态](../CURRENT_STATE.md) · [Hybrid设计](../HYBRID_ISOCHRONE_DESIGN.md) · [2.1失败报告](reports/baidu-v21-live-20260917-network/report.md)
