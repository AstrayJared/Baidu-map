# 15 分钟生活圈算法任务完成文档

编写日期：2026-09-12  
实施分支：`codex/adaptive-isochrone`  
算法开发基线：`d0ca7d2737229295127cfd04170ae0fa714ca3f4`  
推送前同步远端：`278ec69`（N02 后端基础服务）  
当前状态：算法模块与离线验证已完成；真实数据验证和产品接入待实施。

## 1. 任务完成范围

本次依据 `docs/architecture/` 下的《15分钟生活圈_自适应网格算法与测试方案》，完成阶段 1–5 的离线部分，形成可独立运行、测试和替换测时来源的 Python 模块。

| 工作项 | 状态 | 交付内容 |
|---|---|---|
| 远端仓库更新 | 已完成 | 快进更新至上述基线，保留并备份本地算法方案 |
| 核心算法 | 已完成 | 自适应四叉树、共享采样、边界主动补测、范围扩展 |
| 几何重建 | 已完成 | 三角插值、规则栅格、900 秒等值区域、孔洞与多分量、未知遮罩 |
| 测时调度 | 已完成 | 请求预算、QPS、并发、缓存、重试、取消、截止和故障停止 |
| 百度适配器 | 代码及离线契约测试完成 | 参数封装、响应归一化、错误分类、道路端点偏移检查 |
| 对照与消融 | 已完成 | 均匀点阵、32 方向扇形、关闭主动补测、关闭非边界探索 |
| 测试与实验入口 | 已完成 | pytest、Python 接口、CLI、指标文件及几何对照图 |
| 真实百度 API 与社区验证 | 未执行 | 需核验账号权限、QPS、真实中心点及验证数据 |
| FastAPI、真实地图、设施统计、数据库 | 未接入 | 纳入后续计划 |

源码新增于 `life-circle-algorithm`，根目录 `.gitignore` 补充 Python 产物规则。现有前端没有接入本模块。源码、算法方案与完成文档通过 `codex/adaptive-isochrone` 分支统一交付，提交及远端状态以 Git 记录为准。

推送准备时远端已新增 `backend` 基础服务和 `/health` 接口。本模块尚未接入该后端；后续计划中的 FastAPI 工作是扩展现有服务的分析任务能力。

## 2. 已实现的算法

### 2.1 自适应网格算法

处理流程：

```text
输入中心点与预算
→ 校验坐标系和初始化配置
→ 粗网格角点、中心点测时
→ 检查是否需要扩展范围
→ 非边界探索与四叉树细分
→ 边界主动补测
→ 时间场重建和等值区域提取
→ 输出几何、未知区域、质量与调用统计
```

**初始化与共享采样**

- 默认计算范围为中心周围 ±1600 米，划分为 8×8 个 400 米粗格。
- 初始角点 81 个、格中心 64 个，共 145 个位置；起点使用 `T(S,S)=0` 数学锚点，因此无缓存、无重试时需要 144 次外部查询。
- 同一实际请求坐标只测一次；相邻格复用角点、边中点和主动补测点。一次四分裂最多增加 8 个采样位置。
- 不仅检查四角，也检查中心实测值，以发现内部可达通道或超时异常。

**细分决策**

- 细分层级为 400→200→100→50 米。
- 使用可达/超时混合状态、距 900 秒阈值的距离、中心与四角的时间残差、格内时间跨度进行启发式评分。
- 评分为 `U=(L/400)×[4C+2N+2min(Rc/120,1)+min(V/300,1)]`，其中 `C` 表示跨阈值，`N` 表示接近边界的程度，`Rc` 为中心残差，`V` 为时间跨度。
- 跨阈值、距阈值不超过 120 秒、中心残差超过 60 秒或时间跨度超过 300 秒时进入候选队列。并列项按固定空间顺序处理。
- 非边界探索预留总预算的 10%，仅选择样本有效且在阈值同侧、未进入主队列的粗格；随机种子固定为 `20260911`。无合适格子或余额不足时归还主队列。

**边界主动补测**

- 对不超过 100 米的候选格，优先在相邻有效样本跨越阈值的边段上补测。
- 按 `α=(900−TA)/(TB−TA)` 预测位置，并将 `α` 限制在 `[0.1,0.9]`。
- 与已有样本的间距至少 25 米，单个叶格边最多增加两个主动点，共享边统一使用这些点。
- 补测残差目标为 30 秒。明显偏差未解决时继续细分，达到限制后保留不确定性。
- 新点实际参与时间场重建；不假设一条边或一个方向上的步行时间单调。

**范围扩展**

- 外围样本出现不超过 1020 秒的有效时间时，检查是否需要扩展。
- 默认最多扩至 ±3200 米，复用原有采样，完整新增外圈最多需要 400 个位置。
- 预算不足时保留已有范围并标记可能截断；边缘请求失败标记范围未知，不直接解释为超时。

### 2.2 时间场重建与几何提取

1. 对每个叶格收集全部共享边样本，包含邻接细格产生的中间节点。
2. 连接格中心和相邻边界点形成三角片；仅在三个顶点均有有效耗时时进行重心坐标线性插值。
3. 将时间场采样到默认 25 米规则栅格，未知节点影响的保守遮罩一并计入未知区域。
4. 使用 ContourPy 的 `filled(-1, 900)` 提取可达区域，参数固定为 `serial`、`corner_mask=False`、`quad_as_tri=True`、`OuterOffset`、线性插值和单分块。
5. 使用 Shapely 处理并校验几何，最终结果裁剪到有效支持区域。

保留孔洞和多个多边形分量，不采用凸包、平滑、填洞或按面积删除小分量。恰好 900 秒形成的纯点、纯线退化环不构成面；其他无效几何显式返回错误，不静默修补。

### 2.3 对照算法与消融配置

| 名称 | 实现方式 | 主要用途与限制 |
|---|---|---|
| `adaptive` | 四叉树、共享采样、主动补测和非边界探索 | 当前主算法 |
| `uniform` | 查询预算可覆盖的最大规则点阵，格中心由四角推导，起点保留零锚点 | 用同一几何提取流程比较采样策略 |
| `radial` | 32 个方向，每方向四个初始距离层，再局部细化最外已知可达区间 | 星形表达不能可靠保留孔洞和非星形结构 |
| `no_active` | 自适应算法关闭主动补测 | 检验主动点的影响 |
| `no_exploration` | 自适应算法将探索比例设为 0 | 检验非边界探索的影响 |

离线实验采用相同中心、±1600 米范围和 200/400/800 次预算，统一关闭范围扩展。每次运行使用独立 Provider 与任务缓存，算法无法读取验证真值或其他算法的采样结果。

## 3. 已实现的接口

当前算法模块接口包括 **Python 库接口、离线 CLI 和百度上游接口适配器**。现有后端的 `/health` 不提供算法计算能力，尚未提供前端可调用的 HTTP 分析服务；下文 Python 函数不能作为已上线的 REST API 使用。

### 3.1 算法计算接口

```python
# life_circle.engine
async def compute_isochrone(
    request, provider, cancel_token=None, *, clock=None, method="adaptive"
): ...

# life_circle.baselines
async def compute_radial(
    request, provider, cancel_token=None, *, clock=None
): ...
```

两者均返回 `IsochroneResult`。`compute_isochrone` 的 `method` 支持 `adaptive`、`uniform`；扇形使用独立入口。两项消融通过请求参数配置，CLI 已封装相应名称。

`clock` 用于注入测试时钟；调用 `CancelToken.cancel()` 可以请求取消任务。输入配置不合法或不足以完成初始化时，计算入口抛出 `ValueError`，不开始调用测时 Provider。

### 3.2 输入对象 `IsochroneRequest`

| 字段 | 默认值/要求 | 含义 |
|---|---|---|
| `origin` | 必填，`(经度, 纬度)` | 分析中心 |
| `coordinate_system` | 必填，仅支持 `bd09ll` | 显式坐标系，不自动猜测 |
| `threshold` | `900`，首版固定 | 15 分钟业务阈值，单位秒 |
| `extent` / `max_extent` | `1600` / `3200` | 局部计算半宽和最大半宽，单位米 |
| `coarse_size` / `min_size` | `400` / `50` | 初始与最小格边长，单位米 |
| `raster_size` | `25` | 输出栅格间隔，单位米 |
| `boundary_band` | `120` | 阈值附近的关注带宽，单位秒 |
| `residual_target` / `min_spacing` | `30` / `25` | 主动补测残差目标（秒）和间距下限（米） |
| `budget` | `400` | 实际调用上限，包含失败和重试 |
| `exploration_fraction` | `0.1` | 非边界探索预算比例 |
| `max_attempts` | `2` | 首次调用加最多一次重试 |
| `timeout` / `deadline_seconds` | `8` / `600` | 单次超时与相对任务截止时间，单位秒 |
| `concurrency` / `qps` | `2` / `None` | 并发上限；真实 Provider 必须显式配置 QPS |
| `seed` | `20260911` | 探索顺序的固定随机种子 |
| `active_sampling` / `expand` | `True` / `True` | 主动补测与范围扩展开关 |
| `config_version` | `adaptive-v1` | 配置版本标记 |

内部局部米制坐标仅用于采样尺度和几何计算，不是 BD09 到 WGS84 的转换。发往上游的经纬度归一化到六位小数，缓存按实际请求坐标去重。

### 3.3 测时与调度接口

```python
# Provider 协议：由具体适配器实现
async def query_walking_time(origin, destination, deadline): ...

# Scheduler：统一执行缓存、预算、限速、重试和停止规则
async def query(self, destination): ...
async def observe_many(self, destinations, *, request_limit=None): ...
```

- Provider 返回 `RouteObservation`，声明固定 `identity` 和 `network` 属性；`deadline` 为单调时钟上的绝对截止时间。
- `AnalyticProvider` 根据局部坐标调用合成时间函数；`BaiduProvider` 封装百度步行路线接口。
- `Scheduler.query()` 返回单个观测；`observe_many()` 按输入顺序返回观测列表。`request_limit` 用于阶段内预算上限，例如限制探索及其重试的消耗。
- 预算在实际调用前占用，缓存命中不占新预算；每批按固定顺序汇总结果后安排后续调用。跨任务缓存默认关闭。
- 超时、临时服务错误和限流最多重试一次；权限和参数错误不重试；配额耗尽、连续五个位置用尽尝试仍临时失败、取消或截止会停止新增调用。
- 迟到响应不会改写已结束结果；不合作的 Provider 不能通过取消后继续调度而突破并发限制。

`RouteObservation` 字段如下：

| 字段 | 说明 |
|---|---|
| `destination` / `request_origin` | 实际请求终点及起点；调度器统一补齐请求起点 |
| `duration` / `reason` | 有效秒数，或未知原因；失败不编码成大秒数 |
| `collected_at` / `attempts` | 采集时间与实际尝试次数 |
| `endpoint_verified` | 是否具备道路端点核验信息 |
| `route_origin` / `route_destination` | 可获得的实际道路端点 |
| `reachable` | 计算属性：`True`、`False`、`None`，分别为可达、超时、未知 |

899 秒和 900 秒可达；900.1 秒超时。缺失、负数、非有限或非法类型的耗时为未知，不进入数值插值。

### 3.4 百度上游适配接口

代码中封装的上游地址为：

```text
GET https://api.map.baidu.com/directionlite/v1/walking
```

适配器显式设置 `coord_type=bd09ll`、`ret_coordtype=bd09ll`、`steps_info=1`，起终点按上游要求使用“纬度,经度”。有效路线取最小耗时；首末路线分段端点与请求位置偏移超过 50 米时，该路线不作为原位置的有效证据。响应回显坐标不等于实际道路端点；端点缺失时保留未核验标记。

凭据通过显式参数或环境变量 `BAIDU_MAP_AK` 提供，不写入文档、日志或夹具。首版未实现 SN 签名；内建 CLI 不提供真实请求模式。`BaiduProvider` 应通过异步上下文管理器使用，或由调用者传入并管理 HTTPX 客户端。

当前只使用构造响应完成契约测试，尚未验证真实服务端权限、QPS 和现场响应。目的设施 UID 可用于独立测时，不能绑定到整片区域采样。

### 3.5 输出对象与 JSON 字段

调用 `result.to_dict()` 获得可序列化的业务对象。顶层及几何对象带 `coordinateSystem: "bd09ll"`，不可作为标准 WGS84 GeoJSON 直接使用。

| Python 字段 | JSON 字段 | 内容 |
|---|---|---|
| `geometry` | `geometry` | 估计可达 `MultiPolygon`，允许孔洞和多个分量 |
| `uncertain_region` | `uncertainRegion` | 边界或端点核验存在不确定性的区域 |
| `unknown_region` | `unknownRegion` | 无足够有效支持的区域，包含保守栅格遮罩 |
| `computation_extent` | `computationExtent` | 本次实际计算范围 |
| `quality` | `quality` | `usable`、`partial`、`insufficient` |
| `stop_reason` | `stopReason` | 预算、分辨率限制、最大范围、取消、截止、配额、权限、上游故障或几何错误等停止原因 |
| `statistics` | `statistics` | 请求、缓存、失败、耗时、格层级、未知面积等统计 |
| `warnings` | `warnings` | 截断、范围未知、未完成边界、端点未核验等标记 |
| `config` | `config` | 本次请求与配置快照 |

顶层业务字段采用上述名称；`statistics` 和 `config` 内部字段保留 Python 的下划线命名。`local_geometry`、`local_unknown` 是用于离线评估的局部米制 Shapely 对象，不进入 JSON。

有效计算得到空区域时，返回空 `MultiPolygon`；证据不足或几何错误不能生成结果时，`geometry` 为 `null`。两者不可混同。

质量状态含义：

- `usable`：完成当前搜索范围和分辨率下的既定检查，无已知数据缺口或范围截断，不表示证明了全域正确。
- `partial`：有可用结果，但存在未知、未完成边界检查、可能截断或其他质量提示。
- `insufficient`：证据不足或几何错误，不能生成可用区域。

主要统计包括 `requests`、`network_requests`、`unique_positions`、`cache_hits`、`retries`、`failures`、`latencies`、`total_seconds`、`compute_seconds`、`network_wait_seconds`、`cells_by_size`、`unknown_area`、`unfinished_boundary`、`active_points`、`exploration_requests`。并发请求的延迟累加不等于任务墙钟耗时。

### 3.6 离线 CLI 与调用示例

以下命令使用本机已验证的 D 盘环境；完整安装步骤见 [README](README.md)。

```powershell
Set-Location 'C:\Users\adimn\Desktop\1012\life-circle-algorithm'
$algorithmPython = 'D:\CodexCaches\baidu-map-algorithm-venv\Scripts\python.exe'
$env:PYTHONPYCACHEPREFIX = 'D:\CodexCaches\baidu-pycache'

# 单个合成场景，可切换 adaptive/uniform/radial/no_active/no_exploration
& $algorithmPython -m life_circle compute --scenario plane --method adaptive --budget 400 --output D:\CodexOutputs\baidu-map-plane.json

# 三算法与两项消融的完整实验
& $algorithmPython -m life_circle benchmark --budgets 200 400 800 --output D:\CodexOutputs\baidu-map-algorithm-final
```

Python 最小调用示例：

```python
import asyncio
import math
from life_circle.engine import compute_isochrone
from life_circle.models import IsochroneRequest
from life_circle.providers import AnalyticProvider

request = IsochroneRequest((116.4, 39.9), "bd09ll", budget=400)
provider = AnalyticProvider(request.origin, lambda x, y: math.hypot(x, y) / 1.2)
result = asyncio.run(compute_isochrone(request, provider))
payload = result.to_dict()
```

示例中心只是合成场景的坐标原点，不代表已确认的真实社区。

## 4. 已有验证结果

下列数据引用本次实现的 [测试记录](TEST_REPORT.md)，本次编写完成文档未重新运行算法测试或实验。

| 验证项 | 结果 |
|---|---|
| Python 单元、几何、契约与实验入口测试 | 67 项通过 |
| 前端更新后基线测试 | 7 个测试文件、67 项通过 |
| 前端生产构建 | TypeScript / Vite 构建通过 |
| 合成实验 | 15 个场景 × 3 档预算 × 5 种配置，共 225 组 |
| 请求预算 | 225 组均未超过各自预算 |
| 真实网络请求 | 0 次 |
| 实验几何错误 | 0 组 |
| 匀速平面，自适应算法，800 次预算 | IoU 0.999441，双向边界 P95 0.658 米，达到初始工程门槛 |

覆盖 UT-01～UT-17 和 IT-01，包括未知区域不填充、中心异常、非单调边、共享边一致性、阈值退化几何、预算竞争、取消、迟到响应和绑路偏移。设施路线核验 IT-02 和前后端旧任务隔离 UI-01 尚待接入阶段验证。

已知限制与证据边界：

- 匀速验收通过不代表真实社区精度，也不保证 25 米边界误差；25 米只是默认输出栅格间隔。
- 围墙入口在 200 次预算下 IoU 约 0.8845；20 米通道偏移 37 米时 IoU 约 0.8873，低预算可能遗漏细节。
- 局部采样失败仍保留未知缺口，并将相应漏纳计入误差。
- 本轮 225 组场景没有触发符合默认条件的非边界探索格；消融未提供探索策略的精度贡献证据。额外检查已验证探索调度路径可执行。
- 不能宣称自适应算法普遍更优或更省调用；例如扇形基线在匀速场景用更少请求也达到了工程门槛。

## 5. 后续实施计划

以下为待实施工作，不计入本次完成项；不预设人员或排期。每阶段先补验收用例，再开发并记录结果。

| 顺序 | 阶段与工作 | 前置条件 | 阶段验收 |
|---|---|---|---|
| 1 | 补强离线证据：新增可触发探索的隐藏分量/通道场景，扩大网格偏移与故障覆盖，重新比较两项消融 | 复用现有模块与测试框架 | 探索确实触发；分别记录收益、成本及无收益案例；既有回归门槛保持通过 |
| 2 | 真实百度契约核验：少量请求验证权限、限速、分段端点和错误响应；建立脱敏回放夹具 | 已配置服务端凭据、已确认权限与实际 QPS、明确核验预算 | 参数和响应口径经真实调用确认；凭据不进入日志与夹具；权限与配额错误可正确停止 |
| 3 | 真实社区对比：先选择一个确认的中心点，再增加桥梁绕行、封闭街区等场景 | 真实 BD09LL 中心、测试预算、阶段 2 通过 | 三种算法采用一致口径；建立独立验证集；逐场景报告精度、未知和实际成本，保留失败案例 |
| 4 | FastAPI 分析任务服务：创建任务、查询状态、获取结果、取消任务；封装算法和 Provider | 算法/上游契约稳定，明确服务配置与任务资源限制 | 结果关联正确任务和中心点；取消后不再新增调用；终态稳定；进度展示采样数、预算和阶段，不伪装成准确完成百分比 |
| 5 | 真实地图与前端联调：替换模拟服务，展示多分量、孔洞、未知、不确定区域及质量状态 | 阶段 4 的服务契约可用，具备真实地图接入配置 | 不将真实几何压回示意单环；切换中心后旧任务不覆盖新结果；UI-01 通过 |
| 6 | 设施独立测时与统计：复用调度能力逐设施验证；圈内位置不直接等于路线可达 | 设施来源、类别、UID、去重和测时预算明确 | ≤900 秒计可达，超时不计，未知独立统计；IT-02 通过；统计与报告口径一致 |
| 7 | 持久化与运行保障：按实际需要保存任务、配置版本、统计和结果，补充资源限制及运行监测 | 已明确部署环境、数据保留要求和真实负载 | 任务与结果可追溯；故障可定位；依赖与大数据按 D 盘约定管理；再评估跨任务缓存和数据库方案 |

真实验证建议按原算法方案建立两组独立点集：100 个全域分层点、100 个出入口/桥梁/边界挑战点，分别报告，不直接混合成总体准确率。调参与最终评估使用不同数据；与算法样本重合的位置按固定替补规则处理。

算法分析任务的 HTTP 路由、鉴权、任务持久化结构和数据库迁移尚未实现，应在阶段 4/7 开始时基于现有后端形成单独接口契约。当前不为这些未实现能力指定看似可调用的地址。

## 6. 交付与查阅入口

| 内容 | 位置 |
|---|---|
| 本完成文档 | `life-circle-algorithm/TASK_COMPLETION.md` |
| 安装、运行及开发说明 | [README](README.md) |
| 测试记录、失败案例及覆盖表 | [TEST_REPORT](TEST_REPORT.md) |
| 数据对象与 Provider 协议 | [models.py](src/life_circle/models.py) |
| 自适应与均匀算法入口 | [engine.py](src/life_circle/engine.py) |
| 扇形基线 | [baselines.py](src/life_circle/baselines.py) |
| 测时与百度适配器 | [providers.py](src/life_circle/providers.py)、[scheduler.py](src/life_circle/scheduler.py) |
| 网格与时间场 | [mesh.py](src/life_circle/mesh.py)、[field.py](src/life_circle/field.py) |
| 合成场景、评估与 CLI | [scenarios.py](src/life_circle/scenarios.py)、[experiments.py](src/life_circle/experiments.py)、[cli.py](src/life_circle/cli.py) |
| 依赖定义与锁定文件 | [pyproject.toml](pyproject.toml)、[uv.lock](uv.lock) |
| 完整实验产物 | `D:/CodexOutputs/baidu-map-algorithm-final` |
| Python 测试记录 | `D:/codex-test-artifacts/baidu-algorithm-tests.xml` |

实验产物包含中文 `report.md`、`metrics.json`、`metrics.csv`，以及每个场景/预算/配置对应的业务 JSON 和 SVG 几何对照图。环境、下载缓存与较大实验数据位于 D 盘。
