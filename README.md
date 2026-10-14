# 15 分钟生活圈

基于百度步行路线与 OSM 路网的生活圈分析工作台。支持百度边界搜索（E8.2）与 OSM＋百度（Hybrid）两种引擎，提供设施检索、服务覆盖、疑似缺口地图和冻结报告。

[![Submission checks](https://github.com/AstrayJared/Baidu-map/actions/workflows/ci.yml/badge.svg?branch=codex%2Fsubmission-check)](https://github.com/AstrayJared/Baidu-map/actions/workflows/ci.yml?query=branch%3Acodex%2Fsubmission-check)

[评审启动指南](docs/评审启动指南.md) · [验收报告](docs/提交收尾验收报告-2026-10-14.md) · [MIT 许可证](LICENSE) · [第三方许可与数据来源](THIRD_PARTY_NOTICES.md)

## 环境与依赖

| 环境 | 要求 |
| --- | --- |
| Git | 用于克隆源码和提交改动 |
| Python | 3.11+；本机及 CI 验证使用 3.12，建议使用相同版本 |
| Node.js | `^20.19.0` 或 `>=22.12.0`；CI 使用 Node 24 |
| 网络 | 首次安装依赖需要访问软件源；真实地图、设施和路线服务需要访问百度 |

启动器会创建后端虚拟环境，按 `backend/requirements.lock.txt` 安装依赖、安装本地算法包，并按前端 `package-lock.json` 安装依赖。评审导入运行包无需安装 Pyrosm，也无需从原始 PBF 重新构图。保留锁定依赖文件。

## 克隆、配置并启动

```powershell
git clone --branch codex/submission-check https://github.com/AstrayJared/Baidu-map.git
cd Baidu-map
python dev.py setup --sample-package "附件所在目录/shanghai-20260912-runtime.zip"
python dev.py doctor
python dev.py
```

运行包 ZIP 和 `.zip.sha256` 必须放在同一目录。`setup` 按提示准备配置、密钥和依赖；`doctor` 检查依赖、端口及路网附件就绪状态。默认页面为 <http://127.0.0.1:5173>，后端为 <http://127.0.0.1:8000>，健康检查为 `/health`。Ctrl+C 停止服务。

也可双击 Windows 的 `start.bat`、macOS 的 `start.command`，或在 Linux 执行 `./start.command`。这会进入同一启动流程；完整样例仍需先导入路网附件。

### 用户自行配置 AK

启动器会从样例创建以下本地配置文件，并提示填写 AK：

| 配置文件 | 配置项 | 用途 |
| --- | --- | --- |
| `backend/.env` | `BAIDU_MAP_AK` | 百度服务端 AK：地点检索、步行路线、官方 Geoconv 坐标转换 |
| `life-circle-demo/.env.local` | `VITE_BAIDU_MAP_AK` | 百度浏览器 AK：底图、地点搜索和定位 |

AK 由使用者自行在[百度地图开放平台](https://lbsyun.baidu.com/apiconsole/key)申请，并配置对应服务权限及 IP/Referer 等校验规则。服务端 AK 仅保存在后端；本地配置文件已被 Git 忽略。修改配置后重启服务。

真实分析使用 `ANALYSIS_PROVIDER=baidu`。`synthetic` 是离线测试替身，不产生真实分析结论；它不会让浏览器百度地图自动离线。缺少 AK 时会明确提示相关服务不可用。

应用默认限制为：每轮 POI 默认及上限 1200 次、路线核验累计上限 120 次；坐标转换独立限制默认 1 QPS、100 次/日。实际还受本地配置和账号服务权益约束。这些数值是应用限制，不是用户账号的配额承诺；失败尝试计预算，缓存复用不计新增网络调用。

## 离线路网附件

路网随成果材料单独提供，不进入 Git，也不包含密钥、百度 POI 缓存或历史任务。

- 文件：`shanghai-20260912-runtime.zip`（61,628,840 字节）。
- 校验文件：`shanghai-20260912-runtime.zip.sha256`。
- SHA256：`7889770042ae2a98602558579d95e16d78aaa6cd25f1c0f807a64467c12962b5`。
- 数据版本：`geofabrik-shanghai-20260912`；米制坐标 EPSG:32651；步速 1.3 m/s。
- 内容：图缓存、覆盖边界、元数据、风险层、障碍层及适用的复核文件；各文件摘要与来源见包内 manifest。

单独导入及校验：

```powershell
python backend/scripts/setup_osm_region.py import "附件所在目录/shanghai-20260912-runtime.zip"
python backend/scripts/setup_osm_region.py verify "data/osm/regions/实际安装目录"
```

导入先校验，再备份并更新 OSM 相关配置，保留 AK、额度和其他环境变量；失败保留原配置。`python backend/scripts/setup_osm_region.py clear` 仅清除 OSM 配置，不删除数据。打包与维护说明见 [OSM 数据说明](data/osm/README.md)。

纯百度成圈可不安装 OSM；完整的覆盖、灰区与基础三项专项依赖路网。缺图、版本不一致或证据不足会明确降级，不会冒充完整结果。

## 社区演示与结果口径

1. 导入上海运行包，启动页面，点击“国定一社区样例”，选择引擎并“开始体检”。首次加载城市路网可能耗时数分钟，界面显示实际阶段；刷新或切换算法不会自动取消任务。
2. 成圈采用独立的 **900 秒步行**标准。服务覆盖采用 **1000 米、包含等号、100 米不确定带**，不把不确定带当成放宽服务标准。
3. “基础三项专项”分别检查**菜市场、独立药店、小学**。超市、诊所、幼儿园不能替代；一贯制学校须有明确小学教育能力。
4. 检索完整而未找到相关候选时标注“未检索到／疑似缺口”；检索不完整、候选待复核、入口不明或路网证据不足时保留“未知”。圈外附近设施可以为圈内提供服务，但不计入圈内设施数量。
5. 总体分固定为**购物、医疗、教育三大类各 1/3**。专项独立呈现，不混入总体分；旧报告缺少专项时显示未评估。专项地图支持分别筛选，与报告共享修订和结果摘要。
6. 地图选点、百度搜索与定位返回值直接作为 BD09LL。手输 WGS84/GCJ02 时选择来源坐标系并点击“应用坐标”，官方转换成功才更新中心；失败和迟到响应不会覆盖新选点。BD09LL 原样返回，不调用上游。

## 本机验证与验收口径

| 检查 | 最近记录 |
| --- | --- |
| 后端完整覆盖 | 1210 项：1209 通过，1 项可选原始 PBF 构图烟测因未配置 PBF 跳过；无漏跑或未解决失败 |
| 前端单元测试及构建 | 47 个文件、537 项通过；构建通过 |
| 算法回归 | 96 项通过 |
| 界面与默认集成 | 界面 30 项、独立集成 7 项通过；本机浏览器为 Edge |

完整日志口径及已修复问题见[验收报告](docs/提交收尾验收报告-2026-10-14.md)。上述为本机历史实测，不冒充跨平台 CI 结果。

按项目负责人的最新确认：本机真实 POI 配额不作为提交阻塞项，真实用户自行配置 AK；复现事项确认通过；固定疑似缺口样例与独立核验按用户确认通过。原始实测调用数及历史 partial 结果保持原样，没有补写未执行的测试。提交前剩余自动检查以对应提交的远端 CI 结果为准。

## 自动检查与远端 CI

在 `backend` 目录执行：

```powershell
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=logs/submission-tests -o faulthandler_timeout=0
.venv/Scripts/python.exe -m tools.export_contract --check
```

在 `life-circle-algorithm` 目录执行 `../backend/.venv/Scripts/python.exe -m pytest -q`；在 `life-circle-demo` 目录执行：

```powershell
npm test
npm run build
npx playwright install chromium
npm run test:checkup-ui
npm run test:integration
```

Linux/macOS 后端解释器路径为 `.venv/bin/python`。本机有 Edge 时可设置 `PLAYWRIGHT_CHANNEL=msedge`；CI 使用 Chromium。慢用例保留 120 秒 Python 栈，普通界面断言短等待，离线任务终态等待上限 180 秒。

推送分支后进入 [GitHub Actions](https://github.com/AstrayJared/Baidu-map/actions)，选择 **Submission checks**。工作流包含 Linux 后端三组、Windows 后端三组和前端一个任务，共七项；后端任务还运行算法、契约与语法检查。CI 使用合成上游，不需要真实 AK，不访问百度，也不下载真实路网。

所有任务成功后才记录远端通过。失败时查看红色步骤，下载日志、截图或 trace；修复后提交并重新推送。精确源码 SHA、工作流链接和运行尝试由同次运行的 `submission-ci-receipt` 附件提供，可与运行页面的七个任务核对。该记录避免将旧提交的成功结果套用到新提交。

## 已知限制与维护

设施目录未经独立完整性核实；入口、通行权限、路网时效、成圈近似及路线抽检范围仍有限制。未知区域可以合理保留，单条不可达路线不能证明所有候选设施均缺失。OSM 原样水系不等于人工实地复核；报告保留来源和证据等级。

多城市自动化、实时围挡与门禁、矩阵并发提速、人口容量、规划选址和重启续查作为后期方向。

依赖安装失败时先核对 Python/Node 版本和网络，再重跑 `python dev.py setup`；必要时使用 `--index-url` 指定可访问的软件源。不要删除锁文件来绕过依赖冲突。常用维护入口为 `python dev.py doctor`、`python dev.py backend`、`python dev.py frontend` 和 `--no-browser`。

原创源码使用 MIT；OSM 数据适用 ODbL，百度服务和第三方依赖保留各自许可。界面使用 OSM 派生结果时显示 OpenStreetMap 署名。
