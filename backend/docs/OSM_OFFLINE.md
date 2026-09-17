> 当前状态（2026-09-17）：独立 OSM 基线接口继续保留；OSM 图引擎同时供 Hybrid 使用。纯百度与 OSM＋百度两套生产算法并存。

# OSM_OFFLINE Phase 1 实现与验收报告

实现日期：2026-09-14。算法为独立 OSM 离线传统路由基线；不是 ground truth。操作步骤见 [数据与运行说明](../../data/osm/README.md)。

1. **现有架构**：FastAPI 入口 `app/main.py`；Pydantic 真源 `app/contracts.py`；配置 `app/config.py`；统一 `AnalysisResponse/Data/Geometry/Issue/Rules`；状态函数 `map_business_status`。内部 `life_circle.models.IsochroneRequest/IsochroneResult` 为 dataclass，不是 Pydantic。现有坐标模块 `life_circle.coordinates.LocalProjection` 只做 BD09 局部采样缩放，不具备中国坐标系转换。仓库没有统一 engine registry/algorithm enum。原异步分析会串联百度设施查询，因此新增 OSM 独立路由并复用既有响应契约，未改动 `analyses.py`、`analysis.py` 或插值实现。

2. **修改文件**：`.gitignore`；`backend/.env.example`、`README.md`、`requirements.lock.txt`；`backend/app/config.py`、`contracts.py`、`main.py`；`backend/tools/export_contract.py`；由工具生成的 `backend/docs/openapi.json` 和 `life-circle-demo/src/api-contract.ts`。SyntheticRequest 的公共字段提取为 AnalysisRequest，原字段和行为不变。OSM 请求只增加算法 Literal 和固定 900 秒阈值。没有创建另一套响应 Schema。

3. **新增文件**：`backend/app/geo/{__init__,coordinates,projection}.py`；`backend/app/algorithms/__init__.py`；`backend/app/algorithms/osm_offline/{__init__,graph_store,prepare,snap,routing,edge_intervals,polygonize,coverage,engine}.py`；`backend/app/osm_api.py`；`backend/scripts/{prepare_osm_graph,smoke_osm_offline}.py`；`backend/requirements-osm-build.txt`；`backend/tests/test_osm_offline_{core,api,prepare}.py`；`data/osm/{README.md,.gitkeep}`；本报告及脱敏性能证据。原始 PBF/缓存/边界在 Git 忽略目录，未提交。

4. **第三方依赖**：运行新增 NetworkX 3.6.1、PyProj 3.7.2，复用 Shapely 2.1.2 和现有 Pydantic/FastAPI。准备阶段使用 Pyrosm 0.13.1、OSMnx 2.0.7、GeoPandas 1.1.4，传递依赖固定在 `requirements-osm-build.txt`。没有在线 Overpass、PostGIS 或额外路由服务。Windows 缺少 MSVC 时，本次通过校验 SHA256 的 conda-forge cykhash 预编译包完成安装；`pip check` 通过。建议他人用 conda-forge 准备环境，避免本地编译障碍。

5. **数据下载和准备**：使用 [Geofabrik Shanghai](https://download.geofabrik.de/asia/china/shanghai.html) 固定 `shanghai-260912.osm.pbf`，配合同次归档 `.poly` coverage。PBF SHA256 为 `0490e886ef41881928c1b10500ee280ef009a892ac43d8a476fc082894064b82`。下载日、来源、边界 SHA256 在数据 README 和生成 metadata 中。只有人工准备下载环节联网；不自动更新快照。

6. **PBF 生成 graph**：Pyrosm `get_network(network_type="walking", nodes=True, extra_attributes=...)` 使用成熟 walking filter。投影 nodes/edges 到配置米制 CRS，转换成 MultiDiGraph；保留所有组件、孤立节点及环。普通道路双向，显式行人方向标签控制有向边。OSMnx simplify 在关键属性或 OSM way ID 变化处保留节点；已有曲线端点和 barrier 节点也保留。检查简化前后组件数量、长度总和和属性一致性；再逐边验证长度、几何方向和时间。

7. **cache 格式**：版本化 gzip JSON，扩展名 `.osm-cache`；节点、边 key、方向、数值及属性作为 JSON，geometry 为 WKT。不是 pickle，不执行对象反序列化代码。临时文件写完后原子替换。跨独立 Python 进程 roundtrip 测试验证节点/边和所有保留属性一致。

8. **cache 加载**：2.0 起使用线程安全惰性单例；应用启动和 `/health` 只创建 `unloaded` 占位对象，不读 cache。首次显式调用 OSM Offline 或旧 Hybrid 接口时，在工作线程执行一次读 cache→校验→规范几何→构建 STRtree 和 weak component 索引→冻结图；并发请求共享同一加载过程。验证数据版本、CRS、步速/时间、length、geometry、端点、关键属性和记录数量。配置与缓存不匹配会明确返回 insufficient，且只影响显式 OSM/Hybrid 接口。PBF 和 Pyrosm 不参与请求。

9. **CRS**：独立模块完成 BD09→GCJ02→迭代近似 WGS84；PyProj `always_xy=True` 转换为 Settings 的 EPSG:32651，逆过程输出 BD09LL。所有 nearest、projection、距离、substring、buffer、coverage 距离在米制 CRS；拒绝经纬度 CRS 和英尺单位投影。上海 roundtrip 采用 2e-6 度容差，不宣称绝对精确。

10. **nearest edge**：启动时 Shapely STRtree 索引真实 edge geometry。请求时 query_nearest 在米制投影下查找道路；等距离候选采用稳定边顺序；沿 LineString.project/interpolate 得到 P。不会按最近 node 作为正常路径，也不遍历全上海路网。

11. **virtual source**：请求局部的 seeded Dijkstra，不插入真实全局节点。对于有向边 u→v、P 在线性位置 s，seed(v)=(1−s/L)w；仅当对应同物理道路的反向 edge 存在时建立反向 seed。若投影恰在端点，该端点 seed=0，允许正常转入其相邻道路。P 到端点之间的可达片段单独加入 source intervals。

12. **共享 graph 不变**：startup 拷贝/规范化后 `nx.freeze`；请求只读邻接和 edge data。heap、seed、distances、intervals 及输出为局部对象。单请求前后全属性比较、8 次并发请求几何一致性测试通过。冻结结构之外，请求也不写节点或边属性。

13. **方向处理**：虚拟起点与路由遵守 MultiDiGraph 已有方向；反向候选须同 OSM way、同端点、同真实几何，不能连接旁边平行道路或跨层交叉道路。构图遵守 `oneway:foot`（yes/1/true、-1/reverse）及 `foot:forward/backward=no`；汽车 oneway 不自动套用到步行。

14. **Dijkstra**：标准非负权重堆松弛，初始多 seed 带成本；仅入堆 candidate<=B，B=900−snap_distance/speed；不会全图最短路计算。与 NetworkX 真正虚拟节点图做 20 组随机有向多重图 oracle 比较。899.999、900、900.001 秒测试确认闭边界，无额外时间容差。

15. **可达区间数学与代码**：`edge_intervals.reachable_intervals` 对每个有向 edge 计算 `[0,L*clamp((B−d(u))/w,0,1)]`。虚拟源对 snapped edge 补充 `[s,min(L,s+LB/w)]`。同向区间先合并，再截取实际几何，最后几何 union；正好预算端点可作为 Point 保留。只遍历 settled nodes 的出边与 source edges。

16. **双侧 partial**：反向 edge 独立计算，从 v 出发的 prefix 经自身反向几何落在物理道路右侧，与 u 侧片段 union。d(u)=850、d(v)=860、w=200、B=900 时返回两端 50+40 的区间，总长 90，中点不可达；绝不因为两端节点都 settled 就填满整条边。

17. **弯曲 edge**：Shapely substring 沿累计弧长截取。startup 比较 geometry 两端与 u/v 的端点匹配，必要时 reverse；端点明显不匹配则拒绝 cache。geometry 缺失只对可信端点使用直线 fallback，并标记 diagnostics/降级；损坏 cache 中丢失 WKT 则整体拒绝。

18. **polygonization**：network union→metric buffer→make_valid→只保留 Polygon/MultiPolygon，验证非空和有效性。buffer 默认 25 米，是展示宽度。内部 debug 返回 reachable network/snap point/metric polygon；公共 Data 未伪造插值 uncertain/unknown/computation extent。输出面不能替代精确路网时间查询。

19. **extract boundary**：支持 WGS84 GeoJSON Polygon/MultiPolygon/Feature/FeatureCollection 和 Geofabrik `.poly`（含洞）。先检查原点是否被 coverage covers；路网接近边界≤margin（默认 100 米）或越界，标记 `graph_coverage_boundary`。不用节点度数推断缺数据。无 coverage、损坏或快照 hash 不匹配时标记检查不可用并降级，不假装边界完整。

20. **quality 与 status**：复用 usable/partial/insufficient 语义。usable 要求生成几何且未发现降级原因；边界截断、coverage 不可用、极小组件或 fallback→partial；缺数据、snap 失败、几何计算失败等→insufficient。已有业务映射把 insufficient→failed；设施未执行时 usable 仍是业务 partial。不会新增 success，也不会把缺数据解释为 empty。

21. **diagnostics**：算法和快照版本、图节点/边数、输入/路由/metric/输出 CRS、snap edge/distance/time、network budget、组件大小、reachable nodes/full edges/partial edges/segments/length、geometry fallback/reverse 计数、coverage 可用性与边界命中、步速/buffer/snap max/coverage margin、polygonization 方法、snap/routing/interval/polygon/total 毫秒、零网络调用及 attribution。所有正常结果会记录这些信息，异常结果至少记录失败阶段/原因及已完成数据。边数统计在启动时完成，不在每次请求扫描全图。

22. **API 调用**：`POST /api/v1/analysis/osm_offline`，JSON `{"origin":{"lng":121.51392519758,"lat":31.313079085826},"coordinate_system":"bd09ll","algorithm":"osm_offline","threshold":900}`。现有 422/500 统一 envelope；没有要求百度 AK。`algorithm.algorithm` 标识算法，`algorithm.quality` 和外层 status 分离。命令与配置见数据 README。

23. **新增测试**：指定核心 16 项全部存在：coordinate_roundtrip、edge_cost、threshold_900_inclusive、basic_cutoff_dijkstra、snap_middle_of_edge、directed_snap、single_sided_partial_edge、boundary_edge_two_sided、curved_partial_edge、edge_geometry_orientation、disconnected_graph、snap_cost_consumes_budget、graph_cache_roundtrip、extract_boundary_partial、polygon_validity、global_graph_immutability。额外测试随机路由 oracle、平行道路隔离、MultiPolygon、非米制 CRS 拒绝、缺失属性/缓存/几何、coverage holes、API 离线/422/500、并发/一次加载、属性安全简化和 PBF 集成。

24. **测试结果**：完整 backend 回归 **343 passed、1 skipped，56.12 秒**；独立算法包 **96 passed，20.54 秒**。最终接口导出修订后，OSM 定向测试加既有业务契约测试 **53 passed、1 skipped，5.69 秒**（其中 OSM 52 项）。跳过项是未设置本地 PBF 时的可选集成，另行显式运行。前端 TypeScript 编译、`pip check` 和 `git diff --check` 通过。FastAPI/Starlette 有既有弃用提示；算法包测试缓存目录有写入提示，但全部断言通过。

25. **真实上海 smoke**：已用上述真实 PBF 完成构图：原始 755,478 节点/1,684,036 有向边；简化后 **274,098 节点/721,276 有向边**；构图阶段约 127.5 秒（不含后续 cache 全校验与写入）。真实缓存 FastAPI smoke 禁止 socket 连接，5 次请求均生成有效 geometry、quality=usable、坐标一致、网络调用 0；业务 status=partial 符合设施未执行的契约。

26. **单次请求性能**：第一轮 5 次为 914.6/876.7/874.1/881.4/852.6 ms；首次 cache 加载、验证和索引约 74.1 秒。移除每请求全图计数后复测为 **552.7/563.9/557.8/533.6/534.0 ms**，geometry 仍完全一致。首轮 snap 0.22 ms、Dijkstra 0.56 ms、区间 23.96 ms、polygon 577.96 ms；149 个 reachable nodes、435 个有向片段，network 总长度约 24.41 km。最终证据见 [OSM_OFFLINE_smoke.json](OSM_OFFLINE_smoke.json)。第二轮与真实构图同时运行，资源竞争下 startup 为 445.5 秒，不能当作独占启动性能；之后集成测试改为串行。结果依赖地点、硬件及内存压力，仅作为实测，不宣称 SLA。

27. **已知限制**：OSM 路网/门禁缺失；近似中国坐标转换；离路直线 snap 不能证明无墙/河阻挡；条件通行、步行 turn restrictions 和开放时间未建模；coverage 只检测数据边界而非内部完整性；固定速度无 penalty；buffer 可能填入非真实可达的面积或掩盖狭窄间隙；上海图缓存首次显式加载较慢且占较多内存，每 worker 独立载入；缓存 v1 只保留 routing 必要属性；前端尚需接入 OSM 展示和 attribution。Graph 中零长度道路准备阶段明确拒绝，不静默删除拓扑。

28. **未来 Comparator**：作为算法外部模块，分别获取 interpolation 与 osm_offline 输出并统一到同一米制 CRS，比较 IoU/交并面积/面积比/对称差/Hausdorff；冻结 development set 调整后的 buffer；使用 held-out 百度步行时间验证点同时评价两算法 Accuracy/Precision/Recall/Boundary Time Error。原始 OSM reachable network 作为可解释诊断保留，不作 ground truth；任何 hybrid 必须是第三种独立算法。

## 验收补充

- 真实上海 PBF 集成测试 **1 passed，467.39 秒（7 分 47 秒）**。覆盖本地 PBF→重新构图→临时 cache→独立读取→应用启动加载→上海坐标计算，几何非空、quality 为 usable/partial、网络请求为 0。该测试单独显式运行，默认 CI 仍跳过本地数据依赖。
- 完整后端回归 343 passed / 1 skipped；原算法包 96 passed；最终契约导出修订定向回归 53 passed / 1 skipped；失败诊断补充后 core/API 43 passed。前端 TypeScript 检查通过。
- 第二轮真实 cache HTTP smoke 5/5 通过，严格禁止 socket 网络连接，几何逐次相同，quality=usable；单次 533.6–563.9 ms。证据在 `OSM_OFFLINE_smoke.json`。
- 准备/集成进程观察到约 7 GB 工作集峰值；常驻城市缓存 smoke 约 1.9 GB。首次独占加载约 74 秒，并发构图时显著变慢。已提前释放准备阶段不再使用的 PBF/GeoDataFrames，仍建议将准备与请求服务分开运行。
- 最后检查确认 PBF、cache、coverage 和生成 metadata 均被 Git 忽略；没有修改插值算法实现；没有提交 Git commit 或启动常驻服务。已有本地数据可直接按运行说明配置环境并启动。

官方资料：[Pyrosm graph 导出与 walking](https://pyrosm.readthedocs.io/en/latest/graphs.html)、[OSMnx simplify_graph](https://osmnx.readthedocs.io/en/stable/user-reference.html)、[Geofabrik 上海数据](https://download.geofabrik.de/asia/china/shanghai.html)、[OSM 版权与 ODbL](https://www.openstreetmap.org/copyright)。
