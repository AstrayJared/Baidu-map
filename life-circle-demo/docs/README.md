# 前端文档索引

`life-circle-demo/docs/` 目前三份文档，都是设计与实现说明；接口的权威定义在后端生成的契约里，这里只讲前端怎么用它。

| 文档 | 内容 |
| --- | --- |
| [前端接口契约.md](前端接口契约.md) | 前端实际使用的三个入口、字段与兼容策略、错误信封与降级。核对基线写在文件开头；改契约要同步改它 |
| [设施分类与UID去重说明.md](设施分类与UID去重说明.md) | 分类词典、UID 去重与圈内判定的前端实现（N13），以及"检索失败不等于零设施"的口径 |
| [位置获取与POI搜索计划书.md](位置获取与POI搜索计划书.md) | Auto-location：浏览器定位、逆地址解析与地图选点的实施计划 |

## 相关入口

- [前端说明](../README.md)：启动、测试与目录结构。
- [本地启动与前后端联调](../../backend/docs/本地启动与前后端联调.md)：前后端一起跑起来。
- [文档总索引](../../docs/README.md)。
- 契约的生成物：`backend/docs/openapi.json` 与 `backend/docs/analysis-response.schema.json`（由 `python -m tools.export_contract` 生成，`--check` 逐字节比对），前端对应的类型是 `src/api-contract.ts` 与 `src/checkup/contract.ts`。

## 与独立 Demo 仓库的关系

介绍站的离线 Demo 是另一个仓库（本地检出在 `intro-page/Baidu-map-demo`，远端 `AegleObserver/our-map-demo`）。它的 `src/` 是本仓库 `life-circle-demo/src` 的同步副本，**不是**自动同步：改动时按那边的 README 列出的例外处理，不要整目录覆盖。本目录的文档只描述本仓库的前端。
