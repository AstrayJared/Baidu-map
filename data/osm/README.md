# OSM 离线数据与可复现准备

Map data © OpenStreetMap contributors. 数据按 [Open Database License (ODbL)](https://www.openstreetmap.org/copyright) 提供。正式前端展示 OSM Produced Work 时必须显示 **© OpenStreetMap contributors** 并链接版权页；当前 API diagnostics 已提供 attribution，前端展示接入是 TODO。

OSM 是独立算法基线，**不是 ground truth**。可能缺道路、门禁、校园出口、小区内部连接。原始可达结果是一维道路片段；buffer 面仅用于显示，不能解释为额外离路步行权限。

数据源：[Geofabrik Shanghai](https://download.geofabrik.de/asia/china/shanghai.html)。本次固定快照为 `shanghai-260912.osm.pbf`，下载日期 2026-09-14；不要每日自动更新。PBF SHA256：

```text
0490e886ef41881928c1b10500ee280ef009a892ac43d8a476fc082894064b82
```

覆盖范围使用同次下载的 Geofabrik `shanghai.poly`（extract 范围，不是从路网死端推断）。SHA256：

```text
c34d551f02a2d42f327e2f123fd2f28399fd914b36ab46f0c099e15b68ae0f59
```

`.poly` 链接会更新；复现实验应保存本地原件并核对上述哈希。使用其他快照时必须一起归档其边界和来源，并重新构图。构图将 coverage SHA256 写入 cache，启动时校验匹配。不匹配时不声称有覆盖完整性检查，quality 降为 partial。

文件均保存在本目录并被 Git 忽略：PBF、`.poly`、GeoJSON、`*.osm-cache`、`graph_metadata.json`。不要提交大数据。项目保留此 README 和 `.gitkeep`。

## 1. 安装

在仓库 `backend` 目录，使用现有环境：

```powershell
$osmPython = (Resolve-Path .venv/Scripts/python.exe).Path
& $osmPython -m pip install -r requirements.lock.txt -e ../life-circle-algorithm
```

运行阶段只需要上述依赖（新增 NetworkX 3.6.1、PyProj 3.7.2；Shapely 2.1.2 已存在）。准备阶段额外需要 Pyrosm 0.13.1、OSMnx 2.0.7、GeoPandas，版本见 `backend/requirements-osm-build.txt`：

```powershell
& $osmPython -m pip install -r requirements-osm-build.txt
```

Windows 上 cykhash 的 PyPI 源码包可能需要 MSVC。没有编译器时优先使用 [Pyrosm 官方建议的 conda-forge 安装方式](https://pyrosm.readthedocs.io/en/latest/faq.html)：

```powershell
conda create -n osm-build -c conda-forge python=3.12 pyrosm=0.13.1 cykhash=2.0.1 geopandas osmnx=2.0.7
conda activate osm-build
python -m pip install -r requirements-osm-build.txt -e ../life-circle-algorithm
```

本次 Windows 测试环境使用 conda-forge 的 `cykhash-2.0.1-py312hbb81ca0_3.conda` 预编译扩展，下载后校验 SHA256，再安装到项目虚拟环境；没有安装系统编译器，也没有改算法来绕过 Pyrosm。准备环境建议使用完整 conda 环境。缓存是数据格式，可以跨准备/运行环境使用。

## 2. 下载并固定数据（仅准备阶段联网）

以下命令从 `backend` 运行。已有本轮文件时无需再次下载。

```powershell
New-Item -ItemType Directory -Force ../data/osm | Out-Null
Invoke-WebRequest 'https://download.geofabrik.de/asia/china/shanghai-260912.osm.pbf' -OutFile '../data/osm/shanghai-260912.osm.pbf'
Invoke-WebRequest 'https://download.geofabrik.de/asia/china/shanghai.poly' -OutFile '../data/osm/shanghai.poly'
Get-FileHash ../data/osm/shanghai-260912.osm.pbf -Algorithm SHA256
Get-FileHash ../data/osm/shanghai.poly -Algorithm SHA256
```

如果 Geofabrik 日快照已经清理，请使用已有归档；若改用其他版本，应更新版本号、来源、下载日期、SHA256，不能冒充此快照。

## 3. 配置与构图

Settings 从 `backend/.env` 读取；环境变量优先。相对文件路径统一相对 `backend` 解析，不依赖运行目录。不要改动已有百度密钥。可在当前 PowerShell 仅设置 OSM 项：

```powershell
$env:OSM_PBF_PATH='../data/osm/shanghai-260912.osm.pbf'
$env:OSM_GRAPH_CACHE_PATH='../data/osm/shanghai.osm-cache'
$env:OSM_DATA_VERSION='geofabrik-shanghai-20260912'
$env:OSM_METRIC_CRS='EPSG:32651'
$env:WALK_SPEED_MPS='1.3'
$env:SNAP_MAX_DISTANCE_M='200'
$env:ISOCHRONE_BUFFER_M='25'
$env:OSM_COVERAGE_BOUNDARY_PATH='../data/osm/shanghai.poly'
$env:OSM_COVERAGE_MARGIN_M='100'
& $osmPython scripts/prepare_osm_graph.py --source 'https://download.geofabrik.de/asia/china/shanghai-260912.osm.pbf' --downloaded-at '2026-09-14'
```

`prepare_osm_graph.py` **不会下载**。读取本地 PBF，通过 Pyrosm `get_network(network_type="walking", nodes=True)`，投影 GeoDataFrames 到米制 CRS，构造保留全部组件的 MultiDiGraph。一般步行道路双向；显式 `oneway:foot`、`foot:forward=no`、`foot:backward=no` 限制方向，汽车 oneway 不自动限制行人。

OSMnx 简化时在 `osmid/highway/foot/access/bridge/tunnel/service/oneway:foot` 变化处保留节点；已有曲线的端点和 barrier 节点也保留；`remove_rings=False`。检查总道路长度、组件数量及属性一致性。`--no-simplify` 可保留所有原节点，默认启用简化。

输出 `shanghai.osm-cache` 与 `graph_metadata.json`：来源、下载日期、文件名、SHA256、构图时间、节点/边数、CRS、速度、简化配置、依赖版本。缓存采用 **gzip 压缩 JSON v1，几何为 WKT**，不会执行 pickle；保存后原子替换文件。加载校验 CRS、版本、cost、geometry、关键属性。速度或数据版本变化必须重建缓存。

缺少 PBF/依赖、无步行道路、损坏几何或零长度道路会使准备失败，并给出明确原因；不会在请求时临时读 PBF 或在线补数据。

## 4. 启动与调用

```powershell
& $osmPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

应用启动不读取缓存。首次显式调用离线 OSM 或旧 Hybrid 接口时，进程内线程安全地加载一次缓存、校验并建立 STRtree/组件索引；请求只读共享图。缺 OSM 缓存不影响默认百度路网 2.0、健康检查或前端启动。

在另一个 PowerShell 窗口请求：

```powershell
$body = @{origin=@{lng=121.51108;lat=31.20415};coordinate_system='bd09ll';algorithm='osm_offline';threshold=900} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/analysis/osm_offline' -ContentType 'application/json' -Body $body
```

请求 Pydantic 模型继承共享 `AnalysisRequest`。响应复用 `AnalysisResponse`、`Data`、`Geometry`、Issue、Rules。算法标识在 `algorithm.algorithm`，质量在 `algorithm.quality`，性能与版本在 `algorithm.diagnostics`。所有输出坐标是 BD09LL，始终 `[longitude, latitude]`。OpenAPI 与 TypeScript 由 `python -m tools.export_contract` 生成，不手工维护重复 Schema。

算法可用且覆盖检查通过：quality=`usable`；边界截断、无可靠 coverage、极小组件或几何直线 fallback：quality=`partial`；没有足够数据、超范围、snap 超限/耗尽预算、缓存错误或几何计算失败：quality=`insufficient`、geometry=null。业务 status 复用既有映射：设施未运行时为 partial，insufficient 时为 failed，**不会把数据缺失映射为 empty**。HTTP 422/500 保持既有 envelope。

## 5. 测试与调试

```powershell
& $osmPython -m pytest tests/test_osm_offline_core.py tests/test_osm_offline_api.py tests/test_osm_offline_prepare.py -q
& $osmPython scripts/smoke_osm_offline.py
& $osmPython -m pytest -q
```

默认 CI 使用合成图，不下载数据；仅在 `OSM_PBF_PATH` 指向实际文件时运行完整 PBF 集成测试。可单独运行 `python -m pytest tests/test_osm_offline_prepare.py::test_real_shanghai_pbf_smoke -q`。真实缓存 smoke 连续发出 5 次 FastAPI 测试客户端请求，禁止 socket 连接，检查几何完全相同并保存性能到 `.tmp/osm-smoke.json`。不需要百度密钥，也不需要启动外部 HTTP 服务。

内部 `OsmComputation` 返回 metric `reachable_network` 和 `snap_point`，`result.local_geometry` 为 metric polygon，可用于调试导出和未来 Comparator；未添加重复公共 API。`data.unknown_region` 等插值特有空间证据为空值，不把未计算的未知范围画成不可达。内部共享 `IsochroneResult` 的这些区域使用空占位，**不表示没有 OSM 未知区域**。

## 6. 解释与限制

1. BD09→GCJ02→WGS84 是近似转换；PyProj 仅负责 WGS84 与当地米制投影。禁止宣称测绘级精确。
2. 起点到最近道路的直线 snap 消耗时间，但无法验证墙、河流或门禁是否阻挡这一离路连接；max distance 只是拒绝过远连接。
3. 可达区间遵守拓扑与图方向，几何相交不自动连通；步行 turn restrictions、条件通行、门禁开放时段等尚未建模。缺失 OSM 标签无法推断。
4. buffer 是展示参数；可能覆盖障碍物、连接很近的道路走廊或掩盖窄小不可达间隙。路由判定应看 reachable network，不能把 buffered 面当作点的真实步行时间证据。
5. 正式实验只能在 development set 调整 buffer；held-out evaluation 冻结参数，禁止以最大化测试集 IoU 调参。
6. Coverage 是 PBF extract 边界，不能证明边界内部 OSM 数据完整。无边界文件时不宣称检查完整，结果降级。
7. 第一版常数速度，无红绿灯/台阶/坡度 penalty；长路中心可能没有可达端点，但仍能输出有效路段。
8. 共享图只在启动时处理。NetworkX 冻结结构，请求不修改节点/边属性；所有 seeds、heap 和 intervals 均为请求局部。城市图占内存较大，多 worker 会重复占用。
9. 本任务未改插值算法、未实现 Comparator/hybrid。未来在算法外比较独立结果；用 held-out 百度验证点同时评价两算法，OSM 不作为真值。
