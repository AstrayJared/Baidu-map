# 介绍站真实截图与帮助页修订（2026-10-11）

**任务**：把 `intro-page/Baidu-map-site` 的首页展示与帮助文档对齐到当前工作台；首屏与四张「操作界面」换成**真实体检**截图。
**状态**：真实体检跑通（业务状态 `partial`），截图已按 3:2 入库，站点检查与构建通过。
**安全声明**：真实调用只有一轮受控体检，用当前长期服务端 AK；未推送、未部署。

---

## 1. 这一轮真实体检（唯一一次花钱的运行）

| 项 | 值 |
| --- | --- |
| 任务号 | `7fc565f1-ceca-4739-9ab9-ab1e8406a2e6` |
| 引擎 / 档位 | `baidu_e82` / 400 |
| 中心 | 121.513925, 31.313079（BD09LL，国定一社区） |
| 起止 | 22:17:15 → 22:21:53（277.6 秒） |
| 状态 | `completed` / stage `ready`，业务状态 **`partial`** |
| 修订 | 5 版：isochrone → poi → accessibility → verification → reporting |
| 已发请求 | 608（成圈、检索、评估、核验合计；方向门不进本应用日账本） |
| 地点检索消耗 | 90 次（应用日账本 `spentToday`，日预算 2000） |
| 实测路线 | **30 条**带实际端点与距离；`strictConfirmed` 0、`pending` 38（`verification-audit.json`） |
| 独立任务目录 | `.tmp/site-shots/checkups`（不写入 `backend/.checkups`，旧任务与修订保留） |
| 配额账本 | 沿用真实账本 `backend/.quota/quota.sqlite3`，因此这一轮的花费是真实记账的 |

复现（在同一台机器上）：

```bash
# 后端（独立任务目录，真实 AK 来自 backend/.env）
cd backend && CHECKUP_DIR=/home/tairitsu_tempest/Baidu-map/.tmp/site-shots/checkups \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log

# 真实验收 + 取图（Chromium 用自带通道；本机没装 Edge）
cd life-circle-demo && PLAYWRIGHT_CHANNEL=bundled CHECKUP_LIVE_ENGINES=baidu_e82 \
  CHECKUP_LIVE_BUDGET=400 CHECKUP_LIVE_OUTPUT_DIR=output/site-shots-20261011 \
  npx playwright test --config playwright.checkup-live.config.ts
```

## 2. 交付的截图与它们的来源

`intro-page/Baidu-map-site/figure/`：

| 文件 | 内容 | 取景 |
| --- | --- | --- |
| `00-overview.png` | 首屏：整屏工作台（设施密度热力） | 同一次运行，按 3:2 居中裁切 |
| `01-select-location.png` | 选点界面（**提交任务之前**，不花额度） | `main.wb-body` 3:2 |
| `02-walkable-area.png` | 地图：15 分钟步行圈 + 服务覆盖热力 | `.api-map-shell` 3:2 |
| `03-facilities.png` | 地图西侧带 + 右侧「周边设施 · 每类最近 5 处」 | 面板与清单同时入画 |
| `04-report.png` | 体检报告抽屉（齐顶 3:2） | `checkup-report` |

全部 3:2（1416×944 / 920×613 / 960×640），逐张核过：不是纯色、底图瓦片已贴上
（选点图中段 6497 种颜色、最常见色占比 48.3%，修好之前是 143 种 / 75.3%，即一张灰底图）。

**这些图不构成精度验收**：它们只说明界面长什么样。精度证据在同一次运行落盘的
`snapshot.json`、`report.json`、六个图层与 `verification-audit.json` 里。

## 3. 为了跑通这轮真实验收修掉的两处**工具**问题

1. **`channel: 'msedge'` 写死**：本机没有 Edge，整套用例停在浏览器启动，一条断言都跑不到
   （报告里看起来像"真实体检跑不起来"）。`playwright.checkup-live.config.ts` 现在支持
   `PLAYWRIGHT_CHANNEL`，`bundled` 表示用自带 Chromium，并接上离线套件解出的运行库
   （缺 `libasound.so.2` 时同样是启动即失败）。默认值仍是 `msedge`，行为不变。
2. **算法切换的选择器过时**：界面已改成 antd `Segmented`，控件上写的是简称
   （"百度边界搜索"），而用例按能力表里的全称找 `.ant-segmented-item`，必然超时。
   改成点标签上的稳定标识 `algorithm-e82` / `algorithm-hybrid`，并**把切换验证放进
   不花额度的预检用例**：切换控件选错引擎正是"看起来跑通了"的典型来源。

另外把「路线证据」断言移到取图之后：它依赖真实数据（本轮真的取回了路线），失败时
前面该落的证据与站点取图已经落盘。**断言本身没有放宽**，仍要求至少一条带实际端点与
距离的路线，`pending` / deadline 不算实测。

## 4. 站点侧同时修掉的三处过时/缺陷

| 位置 | 问题 | 处理 |
| --- | --- | --- |
| `help/index.html`、`help/操作说明.md` | 写「1 公里判断基于**直线距离**」「坐标是 **WGS84**、纬度在前」 | 两者都是错的：服务标准是 `walking_route` 1000 米（能力表 `rules.distance.metric`），坐标全链路是 BD09LL、经度在前 |
| 同上 | 「跳过浏览器端 AK 回退本地示意图」「Node.js 18+」「OSM＋百度仍可提交」「启动不加载全市图」 | 按当前实现改：地图显示"不可用"但可手输坐标；Node `^20.19.0 \|\| >=22.12.0`；无服务端 AK 时提交前具名拒绝；配了地区包则在启动时后台预热 |
| `scripts/serve.mjs` | 本地预览打开 `/help/`（目录）时 `ReadStream` 抛 `EISDIR`，**整个预览服务退出**；而 Pages 上正常 | 目录按 Pages 行为落到 `index.html`，预览与线上一致 |

帮助页还补了：目录口径「10 大类 / 31 个检索小类，默认核心三类」、两个 AK 变量的写法与
位置、明细到期后只留冻结汇总、覆盖率是区间且未知进入上界、以及"这些 OSM 脚本尚未入库"
的声明（`backend/scripts/setup_osm_region.py` 等 5 个文件目前是未跟踪状态）。

## 5. 验证

| 检查 | 结果 |
| --- | --- |
| 真实体检套件（E8.2、400 档） | **2 passed（5.2 分钟）**，含 30 条实测路线断言 |
| 站点 `npm run check` | `Static site checks passed` |
| 站点 `npm run build` | 通过，`dist/figure/` 收录 5 张图 |
| 本地预览（修好 serve 后） | `/`、`/help/`、`/help`、5 张图、`sample-data.json` 全 200；缺失文件 404；服务日志无崩溃 |
| 帮助页示例 `help/sample-data.json` | 被当前契约模型 `CheckupRequest` 接受：`checkup-v1`、`baidu_e82`、400 档、核心三类 |
| 截止时间文案 | `manager.py:66` `DEADLINE_SECONDS = 1800`、`:607` `+600 if budget > 400`，与帮助页一致 |
| 站点 HTML 结构 | 两个 HTML 文件标签平衡检查：0 错误、0 未闭合 |
| 前端类型检查 | `npx tsc -b` exit 0（含改过的 playwright 配置与用例） |

## 6. 未做与未验证

- 未推送、未部署；两个独立仓库与主仓库都只提交到本地分支。
- 未做浏览器 E2E 之外的视觉回归：我无法直接看图，只做了尺寸、3:2 比例与像素统计
  （非纯色、底图瓦片在场）。**最终观感需要人眼确认**（图片已在交付清单里）。
- 未验证 Windows 用户/系统环境变量里是否存在 `BAIDU_MAP_AK` 覆盖（上一轮的遗留项，属
  Gate B）。
- 未改 `backend/` 任何文档中与本次无关的表述（例如 `backend/.env.example` 里"首次
  Hybrid 请求才加载"这句仍是旧说法，属于后端文档，留给下一轮）。
