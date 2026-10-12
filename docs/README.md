# 文档索引

本仓库的文档入口。迁移按"先建索引、再逐批搬移"进行，**每次搬移都在同一个提交里修相对链接**，并由 `scripts/check_repo_layout.py` 与 [仓库布局 workflow](../.github/workflows/repo-layout.yml) 检查（规则见 [仓库目录整理方案](proposals/仓库目录整理方案.md)）。

## 架构与设计

- [需求分析](architecture/15分钟生活圈_需求分析.md)
- [总体技术实现方案](architecture/15分钟生活圈_技术实现方案.md)
- [自适应网格算法与测试方案](architecture/15分钟生活圈_自适应网格算法与测试方案.md)
- [Hybrid 等时圈设计](architecture/HYBRID_ISOCHRONE_DESIGN.md)

## 运行与实施

- [地区设施检索与体检报告实施方案](operations/地区设施检索与体检报告实施方案.md)：历史实施规格，正文明确写着"不表示功能已经实现"。
- [后端说明](../backend/README.md) · [后端文档索引](../backend/docs/README.md)
- [前端说明](../life-circle-demo/README.md) · [前端文档索引](../life-circle-demo/docs/README.md)
- [算法包说明](../life-circle-algorithm/README.md)（其三份文档的索引在文末）
- [本地启动与前后端联调](../backend/docs/本地启动与前后端联调.md)
- [OSM 地区数据](../data/osm/README.md)
- [历史实验报告](../backend/reports/README.md)：百度路网 2.x 时代的原始报告，不作为当前输出

## 方案与决策记录

- [仓库目录整理方案](proposals/仓库目录整理方案.md)：本次迁移的分阶段方案、引用清单与验收办法。
- [网页化部署建议](proposals/WEB_DEPLOYMENT_PROPOSAL.md)：介绍站与分析前端的职责划分、域名与 CI/CD 规划（2026-09-27）。
- [项目介绍站建设方案](proposals/project-site-package/scheme.md) 与 [介绍总览内容底稿](proposals/project-site-package/outline.md)：两个独立仓库的任务目标、边界与内容底稿（2026-09-27）。

## 状态与证据

- [当前状态](../CURRENT_STATE.md)：当前实现、版本与已知限制。
- [验收证据](../evidence/)：`00`–`11` 与 PR 草稿。
- [参考 PDF](references/ai_15_nIsJ99EN3y.pdf)：外部资料。

## 迁移约定

- `architecture/`：跨模块需求、架构和算法设计。
- `api/`：跨模块接口契约与公开 schema（当前尚无迁移文件）。
- `operations/`：启动、联调、部署与实施方案。
- `proposals/`：方案与决策记录。
- `acceptance/`：可复现的验收记录（本轮 `evidence/` 保持在仓库根目录不动）。
- `references/`：外部资料与原始参考文件。

运行路径（`backend/`、`life-circle-demo/`、`life-circle-algorithm/`、`data/`、启动器与契约生成目标）不在迁移范围内：26 个工具与测试文件按相对层级定位兄弟目录，契约生成器同时写 `backend/` 与 `life-circle-demo/`。
