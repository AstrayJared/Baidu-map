# 当前算法状态

2026-09-17：项目同时保留两套可运行方案，供用户选择和开发对比：

- **纯百度算法**：速度较快，任务接口为 `/api/analyses`，请求使用 `center`、`coordinateSystem`、`budget`、`clientRequestId`。
- **OSM＋百度算法（Hybrid v1.5）**：耗时更长但目标是获得更准确的边界，任务接口为 `/api/v1/analysis/hybrid`，请求使用 `origin`、`coordinate_system`、`config`、`client_request_id`。
- 两个接口拥有独立契约和任务管理器，不能互相复用请求体，也不会静默切换算法。
- 前端保留两个独立页面，并通过顶部算法选择器切换；`VITE_ANALYSIS_MODE=baidu|api` 和 `hybrid` 可指定开发时的初始选择。
- 启动与健康检查不加载城市图；首次 Hybrid 或 OSM Offline 分析才惰性加载。缺失数据保留 `degraded` 标记。
- Hybrid 的设施分析尚未接入；纯百度流程继续保留设施检索、采样点评估和设施路线证据。
- 自动测试使用合成数据，不代表真实社区精度验收。本次冲突整理未新增真实百度调用。

[Hybrid 设计](HYBRID_ISOCHRONE_DESIGN.md) · [后端说明](backend/README.md)
