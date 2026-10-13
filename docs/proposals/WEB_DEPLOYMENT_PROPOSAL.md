# 15 分钟生活圈项目网页化部署建议

> 文档状态：方案草案  
> 适用仓库：`PennEwan/Baidu-map`  
> 编写日期：2026-09-27

## 1. 结论

建议采用 GitHub Actions，但需要明确职责边界：

| 组件 | 建议职责 | 是否适合承载运行时服务 |
| --- | --- | --- |
| GitHub Actions | 自动测试、构建、发布和部署 | 否 |
| GitHub Pages | 项目介绍页、文档页、静态 Demo | 仅适合静态资源 |
| `life-circle-demo` | React 分析工作台和浏览器交互 | 需要独立 API |
| FastAPI 后端 | 分析任务、百度调用、OSM 数据和结果接口 | 需要独立云服务或服务器 |
| GitHub Releases | 版本说明、候选版本和实验版本归档 | 不负责应用运行 |

推荐的最终形态如下：

```text
www.example.com       项目介绍网站、架构、文档、截图和版本入口
app.example.com       React 分析工作台
api.example.com       FastAPI 分析 API
github.com/.../releases 版本发布说明和变更记录
```

GitHub Actions 是 CI/CD 工具，不应被理解为后端托管平台。GitHub Pages 可以托管静态网站，但不能运行 Python、FastAPI、OSM 路网加载或百度服务端调用。

## 2. 当前项目评估

### 2.1 已具备的能力

当前 `life-circle-demo` 已经具备一个功能型分析应用的基础：

- 支持百度边界搜索（E8.2）和 OSM + 百度 Hybrid 两个入口。
- 支持中心点选择、坐标输入、地图展示、任务轮询、取消和结果报告。
- 支持 Demo 模式，可使用固定模拟数据完成纯前端演示。
- 支持未知区域、部分结果、数据不足和算法限制的显式展示。
- 已有 Vitest、Playwright、后端 pytest 和生产构建命令。
- 已发布 GitHub Releases，可作为介绍网站的版本入口。

### 2.2 当前不适合直接公开的部分

当前网站还不能单独承担完整的公网产品服务：

- API 模式依赖独立 FastAPI 服务，GitHub Pages 无法替代该服务。
- 后端任务主要由单进程内存管理，服务重启后任务不会恢复。
- 当前没有登录、身份鉴权和用户级配额控制。
- 当前分析会使用百度调用预算，公网开放可能导致额度消耗和滥用。
- 真实百度端到端调用和独立真实社区精度验收尚未完成。
- 前端入口通过 `VITE_ANALYSIS_MODE` 在 Demo 与分析工作台之间切换，尚未形成独立的介绍首页和路由体系。
- 当前仓库没有 GitHub Actions 工作流、Pages 发布配置或自定义域名配置。

因此，现有网站可以作为“功能 Demo”和“分析工作台”，但还不是完整的产品介绍站和公网生产系统。

## 3. 域名规划

### 3.1 推荐域名分层

项目可以先注册并预定义域名，再据此配置前端、API 的白名单和部署环境。

| 域名 | 内容 | 部署位置 |
| --- | --- | --- |
| `www.example.com` | 项目介绍、功能说明、文档和版本入口 | GitHub Pages |
| `app.example.com` | 可交互分析工作台 | GitHub Pages、Cloudflare Pages 或其他静态托管 |
| `api.example.com` | FastAPI 接口 | Render、Railway、Fly.io、云服务器或容器平台 |
| `docs.example.com` | 可选的完整技术文档 | GitHub Pages 或文档平台 |

`example.com` 仅作为示例，应替换为实际注册的域名。

### 3.2 自定义域名注意事项

- GitHub Pages 介绍站可以配置自定义域名，并启用 HTTPS。
- `www` 子域名通常使用 CNAME 指向 GitHub Pages 站点地址。
- 如果使用 GitHub 默认项目地址，例如 `https://pennewan.github.io/Baidu-map/`，Vite 需要设置 `base: '/Baidu-map/'`。
- 如果使用 `www.example.com` 作为站点根域名，Vite 通常可以使用 `base: '/'`，部署配置更简单。
- `app.example.com` 和 `api.example.com` 必须使用 HTTPS，否则浏览器可能阻止跨域请求或地图能力。
- 百度浏览器 AK 的来源白名单应包含实际的 `app.example.com`，不能只配置本地地址。
- 后端 CORS 只允许实际的应用域名，不应在公网环境使用 `*`。

## 4. 网站内容建议

### 4.1 介绍站首页

介绍站不应直接把分析工作台当作首页，而应先回答项目是什么、解决什么问题以及当前能做到什么。

建议页面结构：

1. **首页首屏**：项目名称、核心问题、15 分钟生活圈概念和“在线 Demo”入口。
2. **产品能力**：中心点分析、15 分钟步行圈、未知区域、设施证据和报告展示。
3. **算法方案**：百度 E8.2、OSM + 百度 Hybrid、离线实验 E8.3 的关系和边界。
4. **交互演示**：嵌入静态 Demo 或跳转到 `app.example.com`。
5. **结果可信度**：明确区分真实计算、合成数据、未知区域、部分结果和实验结果。
6. **系统架构**：React、FastAPI、百度地图、OSM 和算法模块之间的数据流。
7. **版本与路线图**：链接 GitHub Releases，展示稳定版、候选版和实验版。
8. **运行文档**：本地启动、环境变量、测试命令和部署限制。
9. **限制说明**：真实社区精度、API 费用、服务端鉴权和数据完整性的当前状态。

### 4.2 当前应用的定位

现有 `life-circle-demo` 建议作为独立的“产品工作台”，而不是直接改造成所有内容都堆在首页的介绍站。

推荐链接关系：

```text
介绍站首页 -> 在线 Demo -> app.example.com
介绍站首页 -> GitHub Releases
介绍站首页 -> 技术文档
分析工作台 -> API 服务 api.example.com
```

这样可以避免营销内容、实验说明和真实分析任务混在一个页面中，也能避免介绍站因为后端暂时不可用而完全无法打开。

## 5. 部署方案

### 5.1 GitHub Pages 介绍站

建议新增一个静态站点目录，例如：

```text
site/
  index.html
  src/
  public/
  package.json
```

介绍站可以使用独立的 React/Vite 入口，也可以使用其他静态站点工具。它不应直接依赖 FastAPI 才能完成首屏渲染。

介绍站构建时不需要服务端百度 AK，也不应读取 `backend/.env`。

### 5.2 分析前端

分析前端构建时至少需要明确以下变量：

```text
VITE_ANALYSIS_MODE=api
VITE_API_BASE_URL=https://api.example.com
VITE_BAIDU_MAP_AK=<受域名限制的浏览器 AK>
```

这些变量是在构建时注入的。修改 GitHub Actions 中的变量后，需要重新构建前端，不会自动改变已经发布的静态文件。

`VITE_BAIDU_MAP_AK` 会被打包到浏览器代码中，因此它不是服务端密钥。它必须配置来源白名单、服务类型限制和配额限制。真正的百度服务端 AK 只能放在后端运行环境中。

### 5.3 FastAPI 后端

后端应部署到支持 Python、环境变量、HTTPS 和持续运行进程的平台。部署时需要配置：

```text
ANALYSIS_PROVIDER=baidu
BAIDU_MAP_AK=<服务端密钥>
ANALYSIS_QPS=<账户允许的 QPS>
CORS_ORIGINS=["https://app.example.com"]
```

生产后端还需要补充以下能力后再公开：

- 用户或项目级鉴权。
- 请求频率限制和调用预算隔离。
- 任务和结果持久化。
- 服务重启后的任务处理策略。
- 日志脱敏、错误追踪和费用监控。
- 对公网请求的输入大小、坐标范围和任务时长限制。

不能通过前端把 `synthetic` 结果伪装成真实百度结果，也不应在后端不可用时静默回退为模拟数据。

## 6. GitHub Actions 规划

当前建议至少增加以下工作流：

### 6.1 Pull Request 检查

建议文件：`.github/workflows/ci.yml`

检查内容：

- Python 3.11 环境安装后端和算法依赖。
- 运行后端 pytest。
- 运行算法模块 pytest。
- 执行前端 `npm ci`、`npm test` 和 `npm run build`。
- 执行前端分析 UI 和集成测试所需的离线检查。
- 禁止 CI 默认访问真实百度接口。
- 检查 `git diff --check` 和生成的契约文件是否稳定。

### 6.2 介绍站部署

建议文件：`.github/workflows/deploy-site.yml`

触发条件：

- `main` 分支合并后部署测试版。
- `v*` 标签发布后部署对应版本。
- Pull Request 只构建和预览，不直接覆盖正式站点。

基本流程：

1. Checkout 仓库。
2. 安装 Node.js 和前端依赖。
3. 构建 `site/`。
4. 上传静态产物到 GitHub Pages。
5. 使用 GitHub Pages 部署环境发布。

### 6.3 分析前端部署

如果介绍站和分析前端使用同一个仓库，应明确两者的构建目录和发布路径。推荐的初期方案是：

- 介绍站独立仓库或独立 Pages 站点。
- 当前仓库继续维护 `life-circle-demo`。
- 分析前端单独构建并发布到 `app.example.com`。

如果必须放在同一个 Pages 站点，则需要把介绍站和分析应用组织为不同路径，例如 `/` 和 `/app/`，并补充前端路由、静态资源 `base` 和刷新回退配置。当前项目还没有这些配置，不建议第一步就采用此方案。

## 7. 当前代码需要补充的部署配置

正式部署前建议新增或修改：

| 文件或位置 | 建议修改 |
| --- | --- |
| `.github/workflows/ci.yml` | 增加后端、算法和前端自动检查 |
| `.github/workflows/deploy-site.yml` | 构建并发布介绍站 |
| `.github/workflows/deploy-app.yml` | 构建分析前端并发布到应用站点 |
| `site/` | 新增项目介绍网站 |
| `life-circle-demo/vite.config.ts` | 根据部署地址明确设置 `base` |
| `life-circle-demo/.env.example` | 增加正式域名和 staging 配置示例 |
| GitHub Pages 设置 | 配置自定义域名和 HTTPS |
| DNS 配置 | 设置 `www`、`app`、`api` 记录 |
| 后端部署平台 | 配置服务端 AK、QPS、CORS 和运行命令 |
| 百度地图控制台 | 将正式应用域名加入浏览器 AK 白名单 |

不建议把真实密钥写入以下位置：

- Git 仓库文件。
- `VITE_*` 环境变量。
- GitHub Release 文本。
- 测试报告、截图和日志。
- 前端 JavaScript 之外的公开静态产物。

## 8. 分阶段实施计划

### 阶段一：静态介绍站

目标是让任何人可以打开网站并了解项目，不依赖后端。

验收标准：

- 自定义域名可以访问。
- 首页、功能、架构、限制和版本页可正常打开。
- 页面包含在线 Demo 和 GitHub Releases 链接。
- GitHub Actions 能自动构建并发布。
- 页面不读取服务端 AK，不调用真实百度接口。

### 阶段二：静态 Demo

使用当前 `VITE_ANALYSIS_MODE=demo` 构建一个无后端依赖的演示版本。

验收标准：

- 地图、设施筛选、统计、报告和异常场景可操作。
- 页面明确标注为演示数据。
- 手机和桌面宽度下布局正常。
- Demo 不被误认为真实社区分析结果。

### 阶段三：受控 API 环境

部署 FastAPI 和 `api.example.com`，只允许内部人员或受控测试访问。

验收标准：

- `/health`、Swagger 和任务接口可访问。
- CORS 仅允许正式应用域名。
- synthetic 联调通过。
- 真实百度模式只进行小规模人工 Smoke Test。
- 调用预算、QPS、日志和错误状态可追踪。

### 阶段四：公开分析工作台

只有完成鉴权、限流、持久化和真实环境核验后，才允许公开 `app.example.com` 的 API 分析能力。

验收标准：

- 未授权请求不能消耗百度预算。
- 单用户和全局调用预算均可限制。
- 后端重启不会造成不可解释的任务状态。
- 真实结果、部分结果、未知区域和错误均有明确提示。
- 真实社区精度结论有独立验证材料支持。

## 9. 推荐决策

当前最稳妥的路线是：

1. 先注册并确定正式域名，优先确定 `www`、`app` 和 `api` 三个子域名。
2. 使用 GitHub Actions 建立 CI，不等待后端部署完成。
3. 使用 GitHub Pages 部署一个不依赖后端的介绍站和 Demo。
4. 保留当前 `life-circle-demo` 作为分析工作台，不立即把所有内容合并到介绍首页。
5. 独立部署并加固 FastAPI 后端，再接入 `app.example.com`。
6. 公开发布时使用 GitHub Releases 记录稳定版、候选版和实验版，不把实验算法描述为生产能力。

最终判断：当前整个网站已经足够支撑“项目 Demo”和“功能工作台”，但不足以直接承担“项目介绍站 + 公网生产分析服务”。GitHub Actions、GitHub Pages、独立后端和自定义域名组合，是当前项目成本最低且风险边界最清晰的网页化方案。
