# OSM＋百度 Hybrid v1.5：实现与接入约定

本次已回滚到对话前v1.5成圈核心；[回滚核对](backend/docs/HYBRID_ROLLBACK_CHECK.md)。v1.6实验已停用，正方形范围、采样调度和填充算法恢复原版。

本版只生成 900 秒步行圈估计。当前结果和精度结论见 [核验报告](backend/docs/OSM_BAIDU_CHECK.md)。设施检索留在独立阶段；生成圈面不会触发 POI 请求。

## 计算范围与证据

- 起点先按百度六位小数规范化，再经 BD09LL → WGS84 → EPSG:32651 转换。当前数据范围为上海。
- `analysis_half_width_m` 默认 1200，最大 1200；计算域为米制投影中东西、南北各 ±1200 米。较小值仅供受限实验。
- `max_exploration_radius_m` 为保留的附加径向上限，默认 3200；它不能扩大正方形，通常不限制默认方形内的点。
- 默认 16 个方向，400/700/1000/1200 米初探；继续外探受射线与正方形交点限制。四角支撑点向内缩 0.5 米以容纳坐标舍入，二维探索覆盖整个正方形。
- 所有候选及其六位小数请求坐标均在公共入口校验。越界候选不发送、不占预算。成面拒绝越界样本，不通过事后裁剪掩盖越界采样。
- 默认及最大生成预算 400 次，默认 3 QPS，与其他百度任务共用限流器。失败和不确定发送仍占预算；无候选可细化时提前结束。缩小范围不保证用量下降。
- `extent_truncated=true` 表示圈面或有效可达证据触及计算边缘；这不是已确认的 900 秒边界。结果至少为 `partial`。
- 非硬障碍内部采用连续填充。`evidence_geometry`、`inferred_fill_geometry`、`unknown_region` 分别保留，已知负标签与填充冲突单列，不修改原始标签。硬障碍目前为已有 OSM 水体，符合条件且有证据的步行桥保留；未穷尽门禁、围墙等现实障碍。
- 米制几何转成非线性百度坐标时，必要时加密边线，再在原有严格面积容差内修复数值异常，不放宽容差隐藏拓扑变化。

## 独立异步接口

接口前缀 `/api/v1/analysis/hybrid`：

| 方法与路径 | 用途 |
| --- | --- |
| `POST /`（实际无末尾斜线） | 创建任务，返回 202 |
| `GET /{task_id}` | 查询准备、计算或终态 |
| `GET /{task_id}/result` | 获取已完成结果 |
| `POST /{task_id}/cancel` | 停止后续请求 |
| `GET /by-request/{client_request_id}` | 请求响应丢失后找回任务 |
| `POST /by-request/{client_request_id}/cancel` | 按客户端请求标识取消 |

请求示例：

```json
{
  "origin": {"lng": 121.51392519758, "lat": 31.313079085826},
  "coordinate_system": "bd09ll",
  "client_request_id": "my-hybrid-request-001",
  "config": {"analysis_half_width_m": 1200, "max_baidu_requests": 400, "request_qps": 3}
}
```

相同请求标识与输入幂等；相同标识不同输入返回 409 `hybrid_request_id_conflict`。单进程最多一个活动 Hybrid 任务。终态保留 30 分钟、最多 20 条，重启后内存任务清空；找回仅在此窗口内有效，磁盘账本不自动恢复为任务。部署使用一个 worker。运行中断不自动续费重试。

结果外层沿用 `TaskResultResponse` 的字段命名，`algorithm` 和 `isochrone` 均为同一专用 `HybridIsochrone` 内容，内部字段使用 snake_case。不能交给旧网格页面的 `analysis/validate.ts`。使用生成的 `HybridRequest` / `HybridResultResponse` 类型及独立 `src/hybrid/client.ts`；本版未切换现有页面。

- 外层 `coordinateSystem=bd09ll`、`coordinateOrder=longitude,latitude`；几何内同样有 `coordinateSystem`。距离、耗时、面积单位分别为米、秒、平方米。
- 空的结果几何为 `null`；`computation_extent` 始终非空。支持 Polygon / MultiPolygon 及内环。
- `taskStatus=completed` 只表示任务运行完成。`quality` 表示圈面质量；设施未接入时 `facilitiesStatus=not_integrated`，业务状态不能因此冒充完整功能。
- `readiness` 逐项返回图、版本、OSM 覆盖范围、风险层、水体层状态。缺失或存在数据警告时明确标为 `degraded` 并降低质量，不声称完整 Hybrid。在线精度工具要求所有数据可用标志为真，且未解析水线不影响最终圈面；圈面外的水线警告保留，不阻止对已有 partial 圈面做独立测量。
- `config_hash` 对原始请求的起点、坐标系与完整配置取规范 JSON SHA-256（不含客户端请求 ID）；`result_hash` 对已序列化 `isochrone` 取相同哈希。不是数字签名。
- `timing_seconds` 包含准备、水体加载、实际请求响应区间之和、成面、计算与任务耗时。请求耗时不含 QPS 等待；API 任务耗时不含服务启动冷加载。验证工具另记冷加载耗时。
- 完整样本、米制几何与冲突保存在本地 `diagnostics.json` 和 `ledger.json`，不重复塞入公共响应。
- HTTP 错误为 `{code,message}`；422 为 `hybrid_invalid_request`，忙时 409 为 `hybrid_busy`，结果未就绪为 `hybrid_result_not_available`。失败账本仅记录阶段、异常类型和耗时，不记录密钥、完整 URL 或异常原文。

## 可复现验证

在 backend 目录：

```powershell
.\.venv\Scripts\python.exe -m tools.export_contract
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m tools.validate_hybrid --run-live --output .hybrid-ledgers/verification-v15-restored
.\.venv\Scripts\python.exe -m tools.validate_hybrid --summarize --output .hybrid-ledgers/verification-v15-restored
```

前两个命令不调用百度。`--run-live` 是计费入口，要求配置好密钥、OSM 数据及共享 QPS=3，最多生成 400 次、验证 100 次。输出目录的一次性启动标记禁止重跑，即使中断也不移除标记。本轮以用户批准的单地点及总计 500 次为限。

若生成已完成并冻结，但验证阶段尚未启动，可在同目录使用 `--validate-frozen` 首次执行独立验证；不重跑或修改生成结果、不加载全市路网、不追加生成预算。它核对原始任务状态、起点、结果、就绪证据与原始预算；一旦存在验证启动标记或账本就拒绝再执行。原始守卫阻塞记录保留为审计历史。

验证与生成解耦。先冻结圈面、配置和生成账本，再按几何自动分配100个独立核验点：内部推断按面积占比安排3—10点（无推断面时0），其余内部最多5点，远处圈外最多2点，其余全部给边界两侧50米带。空分层转给边界。边界最多20点优先靠近推断或证据薄弱处，其余分散抽取；所有坐标归一化后去重、校验。此调整不改变成圈采样或最终几何。

严格900秒与报告±15秒容差分别保存。885—915秒内的空间分类差异不记误纳或漏纳，无效证据仍为未知。总体空间分类和边界空间分类单列，不混入“点恰好位于900秒等时线”的测时通过率。新方案标记为hybrid-validation-v3，旧v1/v2历史指标按原规则复算，不覆盖旧运行。

新验证要求有效点至少80，各适用分层至少80%有效，可达和不可达各至少20；总体容差一致率≥90%，边界≥95%。这仅是抽样分类验收，不代表所有边界误差≤15秒。旧版没有v1.6的法向未确认边长指标，不套用或虚构该比例。本次回滚没有执行新的独立验证，只用已有账本离线复核。

旧 `scripts/validate_osm_baidu.py` 仅用于显式提供旧 60 点计划的离线 OSM 比较；当前 Hybrid 不依赖已删除的历史报告。旧 hybrid-v1 账本不能直接续跑到 v1.5，保留供历史审计。

## 后续圈内设施查询

新增设施查询服务消费固定的 Hybrid 圈面和结果哈希；不回调生成算法重新造圈。圈面任务过期后，后续服务需从受控结果存储读取或持久化其固定快照，不能要求客户端随意指定服务器路径。

1. 按圈面外包范围和用户类别检索候选，复用 POI 分页、来源记录和 UID 去重。
2. 用同一坐标约定做精确点面筛选，支持内环、多分量及边界点。对位于推断填充内的设施保留标记。
3. 分类目录将医院、诊所、药店、幼儿园、中小学等分开；当前药店和小学枚举不是完整医疗教育目录。具体首批分类在设施阶段定稿。
4. 空间纳入与 900 秒步行确证是两个字段；仅对用户需要确证的设施追加路线请求，已知时携带 POI UID。
5. 检索与路线分别预算、分别记账；按结果哈希、分类配置版本和数据时效管理缓存。查询失败、分页截断和零结果分别表示，不以零结果推导服务缺失或盲区。

参考：[百度地点检索](https://lbs.baidu.com/docs/webapi?title=placev3/guide/webservice-placeapiV3/use)、[步行路线](https://lbs.baidu.com/docs/webapi?title=directionlite/guide/webservice-lwrouteplanapi/walk)。本版不新增设施联网调用。
