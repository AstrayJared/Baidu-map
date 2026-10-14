# 15 分钟生活圈体检前端

当前默认入口为 React/Vite 的体检 v2 工作台，通过 `/api/v2/checkups` 创建、恢复、取消和续查任务。E8.2.1 与 OSM＋百度共用设施、覆盖、专项和报告流程。旧独立分析页面不作为默认入口；显式 `VITE_ANALYSIS_MODE=demo` 仍可运行合成示意演示。

完整环境、离线附件与评审步骤见 [评审启动指南](../docs/评审启动指南.md)。大类总体区间仅按购物、医疗、教育各 1/3；菜市场、独立药店、小学专项单独给出三态面积、疑似缺口及热力。地图与报告均使用服务端冻结修订，历史缺专项时标未评估。

## 配置与启动

浏览器地图 AK 写入 `.env.local` 的 `VITE_BAIDU_MAP_AK`；服务端 AK 只放后端。`VITE_API_BASE_URL=http://127.0.0.1:8000` 用于本地分端口运行。复制 `.env.example`，或从根目录运行 `python dev.py` 自动准备。

```powershell
npm ci
npm run dev
```

地图、定位和地点搜索使用 BD09LL；输入 GCJ02/WGS84 时通过后端官方坐标转换，点击“应用坐标”成功后更新中心，失败不更改位置。可点击“国定一社区样例”，完整覆盖须先导入上海路网包。首次加载显示真实进度。

## 验证

```powershell
npm test
npm run build
npm run test:checkup-ui
npm run test:integration
```

界面套件使用模拟 SDK；集成套件使用真实 FastAPI 和合成上游，外部网络被拒绝，不消耗百度额度。默认 Chromium，可设 `PLAYWRIGHT_CHANNEL=msedge` 使用系统 Edge。首次使用 Chromium 执行 `npx playwright install chromium`，Linux 可追加 `--with-deps`。Python 默认取项目后端虚拟环境，可用 `ANALYSIS_TEST_PYTHON` 覆盖。

## 能力边界

只生成实际计算的 15 分钟近似圈，不伪造 5/10 分钟圈。密度与模型服务覆盖有不同含义；未知不当作已覆盖或缺口。专项候选入口不明时保持未知；检索充分且零候选仅表示疑似缺口。大类核验不自动变成专项实测。目录、人口、容量、准入与规划选址的限制随报告展示。
