# 后端文档索引

`backend/docs/` 存放后端的契约、设计、验收与研发记录：**139 份跟踪文件** —— 根目录 39 份、`assets/` 87 份（验收截图与 HTML 报告，按批次分目录）、`performance/` 13 份（性能数据）。

历史实验报告不在这个目录，另有 [backend/reports](../reports/README.md)（48 份，那里说明了为什么保留）。

## 手不要动的：生成物

| 文件 | 说明 |
| --- | --- |
| `openapi.json`、`analysis-response.schema.json` | 由 `python -m tools.export_contract` 生成，`--check` 会与代码逐字节比对。改契约要改代码再重新生成，不要手改这里 |

## 契约与设计

- **N 系列**：`N01-阶段0口径确认.md`、`N02-验收记录.md`、`N04-算法参数与边界.md`、`N04-N05-验收记录.md`、`N05-接口契约.md`、`N05-接口契约收尾.md`、`N06-步行矩阵适配与错误处理.md`。
- **OSM 与 Hybrid**：`OSM_OFFLINE.md`（离线基线接口）、`OSM_BAIDU_CHECK.md`、`OSM_BAIDU_CHECK_V16.md`、`OSM_BAIDU_NEW_STRATEGY_TEST.md`、`HYBRID_ROLLBACK_CHECK.md`。
- **算法演进**：`BAIDU_ROAD_RETIREMENT.md`（百度路网 2.x 退役）、`当前算法版本_E8.3.md`（POI 引导联合构圈：离线实验，不接生产）、`当前进度与接口报告.md`。
- **接入记录**：`任务B_POI接口与验收说明.md`、`算法前后端接入任务报告.md`。

生产算法的说明在仓库级文档里：[Hybrid 等时圈设计](../../docs/architecture/HYBRID_ISOCHRONE_DESIGN.md)、[当前状态](../../CURRENT_STATE.md)。

## 运行与联调

- `本地启动与前后端联调.md`：启动、环境变量与联调入口（前端文档也从这里链过来）。
- `QPS限流问题报告.md`：限流问题的定位过程。
- `版本同步记录_2026-09-14.md`。

## 验收与研发记录（按时间）

| 日期 | 文件 |
| --- | --- |
| 2026-09-27 | `体检v2-真实验收记录-2026-09-27.md`、`体检v2-真百度AK复验记录-2026-09-27.md`、`体检v2-接力修复与复验说明-2026-09-27.md` |
| 2026-09-28 | `热力图真实底图验收-2026-09-28.md`、`水系复核与真实底图验收-2026-09-28.md`、`体检任务实时进度与生命周期验收-2026-09-28.md` |
| 2026-09-29 | `E8.2.1-研发记录-2026-09-29.md` |
| 2026-10-05 | `设施分类规则约束报告-2026-10-05.md`、`采样预算耗时抽样记录-2026-10-05.md` |
| 2026-10-08 | `设施检索预算与缓存修复-2026-10-08.md` |

这些记录里"真实验收"与"离线验收"是两回事：前者花真实额度、必须写明任务号与调用数，后者用替身与合成数据。测试全绿不代表社区精度已验收。

## 国定一社区专项复核

`国定一社区_数据准确性与预算充分性复核.md`、`国定一社区_自适应800预算测试报告.md`、`国定一社区_自适应算法漏纳与误纳原因分析报告.md`、`国定一社区_六个共同误判点复核报告.md`、`国定一社区_下一阶段实验实施计划.md`。

## 附件与大数据（历史证据，不删）

- `assets/`（87 份，按批次分目录）：`checkup-v2-20260927`、`checkup-v2-live-recheck-20260927`、`checkup-v2-review-20260927`、`checkup-lifecycle-20260928`、`heat-acceptance-20260928`、`water-acceptance-20260928`，另有 4 张单独的 `osm-baidu-*.png`。
- `performance/poi-distance/`（13 份）：检索距离与矩阵的性能数据。最大的三份是 `correctness.json`（3.0 MB）、`matrix-results.json`（2.3 MB）、`assets/checkup-v2-live-recheck-20260927/真实百度AK测试报告.html`（2.0 MB）。
- 中间证据 JSON：`OSM_OFFLINE_smoke.json`、`n04-n05-http-evidence.json`。

不要为了让 `git diff` 好看而删掉或改写这些证据。新增时按"日期 + 主题"命名；真实调用产生的证据要与离线证据分开标注。
