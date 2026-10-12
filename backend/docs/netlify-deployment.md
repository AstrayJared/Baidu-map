# Netlify 公网部署

本项目采用「Netlify 前端 + 独立 HTTPS Python 后端」。只上传前端不会让体检任务、设施检索和报告服务自动上线。

## 1. 后端先准备

后端需要常驻 Python 服务、可写的持久化磁盘，以及当前部署对应的 OSM 缓存和覆盖数据。单进程运行，避免重复任务队列与配额状态。

在服务器的仓库根目录安装：

```sh
python -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.lock.txt -e ./life-circle-algorithm
cd backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

以上为 Linux 启动命令。使用服务器的进程管理器保持服务运行，由 HTTPS 反向代理转发到 8000 端口。实际服务器和域名确定后再填写服务配置。

服务端环境变量使用 `backend/.env.example` 的配置结构，至少落实：

- `BAIDU_MAP_AK`：服务端密钥；按百度控制台要求配置服务器 IP 白名单。
- `ANALYSIS_PROVIDER=baidu`。
- `CORS_ORIGINS=["https://实际站点.netlify.app"]`：精确前端来源，无末尾斜杠；自定义域名需要同时加入。
- `CHECKUP_DIR`、`QUOTA_LEDGER_PATH`、`HYBRID_LEDGER_DIR`：持久化磁盘上的路径，重新部署时保留。
- OSM 缓存、版本、覆盖边界、障碍与风险数据路径：参考 `data/osm/README.md`。缺少这些数据时不能宣称完整空间评估。
- 按账号实际额度配置日限额，包括已生效的 `BAIDU_FALLBACK_PLACE_DAILY_BUDGET`。现有接口没有用户登录鉴权，公开体检会消耗共享百度额度。

确认 `https://实际后端域名/health` 和 `/api/facility-catalog` 能访问。

## 2. Netlify 配置

登录 Netlify，导入 GitHub 仓库 `AstrayJared/Baidu-map`，选择需要发布的分支（本轮为 `feat/facility-checkup-v2`）。根目录 `netlify.toml` 已定义：

| 项目 | 配置 |
| --- | --- |
| Base directory | `life-circle-demo` |
| Build command | `node scripts/check-netlify-env.mjs && npm run build` |
| Publish directory | `dist`（相对 Base directory） |
| Node.js | 24 |

在 Netlify 构建环境变量中设置：

| 变量 | 值 |
| --- | --- |
| `VITE_API_BASE_URL` | 后端 HTTPS 来源，例如 `https://api.example.com`，不包含 `/api` 或其他路径 |
| `VITE_BAIDU_MAP_AK` | 浏览器 JavaScript API GL 密钥 |

`VITE_ANALYSIS_MODE=api` 已写入配置。`VITE_` 变量会被打包到浏览器代码，**不能填写服务端密钥**。浏览器 AK 的来源白名单需包含正式 Netlify 域名。环境变量改变后需要重新构建。

配置检查会拒绝缺少变量、HTTP 地址和常见本地地址；它不证明后端实际可达。部署预览如需调用后端，也需要精确的 CORS 来源和浏览器 AK 白名单。

## 3. 发布后验收

1. 打开正式 HTTPS 页面，确认地图加载成功，浏览器没有混合内容或 CORS 错误。
2. 检查分类目录返回 `poi-categories-v2.2`，任务 API 指向公网后端。
3. 在确认真实 API 调用额度后，运行一次小预算体检，验证状态轮询、报告、刷新恢复与取消；不要把合成演示当作生产结果。
4. 确认后端重启后历史报告仍存在，任务状态符合恢复约定。

当前新增文件完成部署准备；只有拿到实际服务器、账号配置和可访问站点后，才能记录公网发布成功。

官方参考：[Vite 部署](https://docs.netlify.com/build/frameworks/framework-setup-guides/vite/)、[构建配置](https://docs.netlify.com/build/configure-builds/file-based-configuration/)、[Functions 执行限制](https://docs.netlify.com/build/functions/configuration/)。
