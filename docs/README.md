# 文档索引

本仓库的文档入口。**本阶段只建立索引、保留现有文档路径**；之后按模块逐批迁移，每次迁移都要同步修复相对链接并做一次文档链接检查（约定与分阶段方案见 [仓库目录整理方案](proposals/仓库目录整理方案.md)）。

## 当前入口

- [当前状态](../CURRENT_STATE.md)：当前实现、版本与已知限制。
- [需求分析](../15分钟生活圈_需求分析.md)
- [总体技术实现方案](../15分钟生活圈_技术实现方案.md)
- [自适应网格算法与测试方案](../15分钟生活圈_自适应网格算法与测试方案.md)
- [Hybrid 等时圈设计](../HYBRID_ISOCHRONE_DESIGN.md)
- [地区设施检索与体检报告实施方案](地区设施检索与体检报告实施方案.md)：历史实施规格，正文明确写着"不表示功能已经实现"。
- [后端说明](../backend/README.md)
- [前端说明](../life-circle-demo/README.md)
- [算法包说明](../life-circle-algorithm/README.md)
- [验收证据](../evidence/)：`00`–`11` 与两份 PR 草稿。
- [网页化部署建议](../intro-page/WEB_DEPLOYMENT_PROPOSAL.md)：**未入库**（`intro-page/` 已按独立仓库忽略），是否收进 `proposals/` 待定。

## 迁移约定

- `architecture/`：跨模块需求、架构和算法设计。
- `api/`：跨模块接口契约与公开 schema。
- `operations/`：启动、联调、部署与实施方案。
- `proposals/`：方案与决策记录。
- `acceptance/`：可复现的验收记录（本轮 `evidence/` 保持在仓库根目录不动）。
- `references/`：外部资料与原始参考文件。

这些目录暂不承载迁移后的副本。每次迁移必须同步修复相对链接，并完成链接检查后再提交；运行路径（`backend/`、`life-circle-demo/`、`life-circle-algorithm/`、`data/`、启动器与契约生成目标）不在迁移范围内。
