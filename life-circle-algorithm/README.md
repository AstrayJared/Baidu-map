# 15 分钟生活圈算法

独立 Python 模块，实现自适应四叉树采样、步行测时调度、带未知区域的时间场重建及 900 秒可达几何。包含均匀网格、32 方向扇形基线和离线实验入口。现已接入仓库内 FastAPI 和 React 的等时圈链路。

实现依据是 `docs/architecture/` 下的《15分钟生活圈_自适应网格算法与测试方案》。真实百度步行 API 与社区实验、设施统计尚未执行；前后端接入已通过离线验收。

本轮在已有 N02 服务上增加任务接口，详见 [前后端接入任务报告](../backend/docs/算法前后端接入任务报告.md)。`TASK_COMPLETION.md` 和 `TEST_REPORT.md` 保留为算法首轮交付记录。

## 安装与运行（PowerShell）

本机已验证 Python 3.11.15，使用 `uv.lock` 锁定运行与测试依赖。下面显式将环境、下载、临时文件、字节码和测试产物放在 D 盘；源码保留在当前仓库。

```powershell
Set-Location 'C:\Users\adimn\Desktop\1012\life-circle-algorithm'
$env:UV_CACHE_DIR = 'D:\CodexCaches\uv'
$env:UV_PROJECT_ENVIRONMENT = 'D:\CodexCaches\baidu-map-algorithm-venv'
$env:PYTHONPYCACHEPREFIX = 'D:\CodexCaches\baidu-pycache'
$env:TEMP = 'D:\CodexTemp'
$env:TMP = 'D:\CodexTemp'
uv sync --locked --extra test --python 3.11
$algorithmPython = 'D:\CodexCaches\baidu-map-algorithm-venv\Scripts\python.exe'

# 单个合成结果（命令行不提供真实 API 模式）
& $algorithmPython -m life_circle compute --scenario plane --budget 400 --output D:\CodexOutputs\baidu-map-plane.json

# 全部单元、几何、契约和实验入口测试
& $algorithmPython -m pytest -q -o cache_dir=D:/codex-test-artifacts/baidu-pytest --basetemp=D:/codex-test-artifacts/baidu-tmp

# 15 个场景 × 3 档预算 × 5 种配置，共 225 组实验
& $algorithmPython -m life_circle benchmark --output D:\CodexOutputs\baidu-map-algorithm-final

# 缩小复现实验范围
& $algorithmPython -m life_circle benchmark --scenarios plane river_bridge --budgets 400 800 --output D:\CodexOutputs\baidu-map-subset
```

`--basetemp` 是 pytest 专用临时目录，不要改成存放其他文件的目录。运行环境已有 Python 时无需另外下载；在另一台电脑使用时应替换源码路径。

实验输出目录包含 `metrics.json`、`metrics.csv`、中文 `report.md`，以及每次计算的 JSON 业务结果和 SVG 几何对照图。包含匀速 800 预算实验时，CLI 会检查 IoU ≥0.95、边界 P95 ≤75 米，未达到则返回非零退出码。

## Python 接口

```python
import asyncio
import math
from life_circle.engine import compute_isochrone
from life_circle.models import CancelToken, IsochroneRequest
from life_circle.providers import AnalyticProvider

request = IsochroneRequest(
    origin=(116.4, 39.9),  # 经度、纬度；这里仅是合成场景的坐标原点
    coordinate_system="bd09ll",
    budget=400,
)
provider = AnalyticProvider(request.origin, lambda x, y: math.hypot(x, y) / 1.2)
token = CancelToken()
result = asyncio.run(compute_isochrone(request, provider, token))
payload = result.to_dict()
```

- `IsochroneRequest`：中心点、显式坐标系、预算、相对任务截止秒数、采样参数及配置版本。阈值固定 900 秒；不支持的坐标系和不足以初始化的配置会在调用 Provider 前被拒绝。
- `RouteObservation`：实际请求起终点、有效秒数或未知原因、采集时间、尝试次数、可获得的道路端点及是否核验。`reachable` 为 `True / False / None`，900 秒可达，900.1 秒超时。
- `compute_isochrone(request, provider, cancel_token, *, clock=None, method="adaptive", on_progress=None)`：自适应算法或 `uniform` 基线；扇形基线使用 `compute_radial`。可选同步回调接收不可变的 `ProgressSnapshot(stage, requests, network_requests, budget, elapsed_seconds)`，应快速返回且不抛异常；不提供回调时用法不变。
- 进度阶段为 `initializing / expanding / exploring / refining / reconstructing / completed`；采样中更新真实调用计数，不估算完成百分比。几何重建通过 `asyncio.to_thread` 运行，后端仍可响应查询和取消；已运行的线程不能强行中断，取消后其结果由任务服务丢弃。
- `CancelToken.cancel()`：停止新请求；已结束结果不接收迟到数据。Provider 应实现可取消的异步 I/O；拒绝取消的 Provider 会停止后续调度，其迟到返回只被丢弃。
- Provider 契约为 `async query_walking_time(origin, destination, deadline)`，`deadline` 为单调时钟的绝对截止时间；另声明固定 `identity` 与 `network`。任务内 Provider 身份和路线选项不可变。

`IsochroneResult.to_dict()` 输出 `geometry`、`uncertainRegion`、`unknownRegion`、`computationExtent`、`quality`、`stopReason`、`statistics`、`warnings` 和 `config`。几何支持孔洞与多分量，显式带 `coordinateSystem: "bd09ll"`，**不是标准 WGS84 GeoJSON**。

有效计算的空区域是空 `MultiPolygon`；没有足够证据生成区域时为 `null`。`local_geometry` 和 `local_unknown` 仅供 Python 离线评估，使用局部近似米制坐标，不进入业务 JSON。

## 算法与调度口径

- 默认范围 ±1600 米，400→200→100→50 米细分，输出栅格 25 米。初始化 145 个位置，其中起点为零锚点，外部查询 144 次。
- 按方案评分选择细分格；100 米以下优先边界主动补测。共享边保留全部采样，包括不同层级的中间节点；中心扇形三角插值使用这些点重建时间场。
- 10% 探索额度只用于有效样本在阈值同侧且未进入主队列的粗格；固定种子 `20260911`。没有合适格子或不足一组细分时归还主队列，不强制消费探索额度。失败和重试均受探索额度限制。
- 调度按固定点序和小批次执行，批次内并发、按顺序处理结果后再安排重试，避免完成先后改变预算分配。默认最多两次尝试、并发 2、单次超时 8 秒、任务截止 600 秒。
- 实际请求前原子占用预算；缓存命中不占预算。缓存命名空间固定为 Provider 身份/版本/选项、实际起点和坐标系，命名空间内按实际终点去重；相反方向和不同 UID 不共用缓存。跨任务缓存关闭。
- 扩展复用初始样本，完整外圈最多增加 400 个位置；预算不足就保留初始范围并标记可能截断。公平实验统一关闭扩展，扩展逻辑另由测试覆盖。
- ContourPy 使用 `serial`、`corner_mask=False`、`quad_as_tri=True`、`OuterOffset`、线性插值及单分块。支持域外缺口和未知节点造成的保守栅格遮罩均计入未知区域。
- 不使用凸包、平滑、填洞或小面删除。等值点造成的纯点/线退化环不构成面；有面积的无效几何显式报错，不静默修补。

`usable` 只表示完成当前搜索范围和分辨率的既定检查；仍有未知、未完成边界检查或可能截断时是 `partial`。证据不足时是 `insufficient`。25 米栅格是输出分辨率，不是精度保证，也不是每 25 米实测一次。

统计中 `requests` 为 Provider 实际调用次数（合成实验的逻辑成本），`network_requests` 为网络 Provider 调用次数，重试均计入；`unique_positions` 包含零锚点和已注册的未知位置。网络延迟累加可能因并发超过墙钟总耗时，不能把各延迟相加解释为任务运行时间。

## 百度适配器边界

`BaiduProvider` 只在显式实例化时读取环境变量 `BAIDU_MAP_AK`；测试全部使用构造响应和 `httpx.MockTransport`，不读取或请求真实账号数据。真实调用必须通过异步上下文管理器使用，配置已确认的 QPS，首版支持 IP 校验的服务端 AK，不实现 SN 签名。

适配器显式设置 BD09LL、六位小数的“纬度,经度”和 `steps_info=1`。从有效路线中取最短秒数，以首末分段端点检查超过 50 米的绑路偏移；响应回显起终点不作为实际道路端点证据。没有分段端点的有效路线保留未核验标记。单目的设施 UID 只供独立 `Scheduler.query()` 测时，不允许用于整片区域采样。

网络超时、服务内部错误、限流最多重试一次；权限、参数错误不重试；配额耗尽停止。连续五个位置用尽尝试仍发生临时故障时停止新采样。适配器和调度器不记录凭据、原始 HTTP URL 或异常文本。

官方契约来源：[步行路线接口](https://lbs.baidu.com/docs/webapi?title=directionlite/guide/webservice-lwrouteplanapi/walk)、[百度通用鉴权与配额状态码](https://lbsyun.baidu.com/index.php?title=lbscloud/api/geosearch)、[ContourPy 参数](https://contourpy.readthedocs.io/en/stable/api/contourpy/contour_generator.html)。真实权限、限速和响应结构仍需后续实测核验。

## 实验解释

三种方法各自只能查询自己选择的位置，不共享其他算法的样本。匀速及故障场景采用半径 1080 米圆形参考，其余采用独立 10 米参考栅格；河流和围墙是 50 米步长的测试路网，使用最短路真值。窄通道包含 20/50/100 米宽度及 0/37 米偏移。

均匀基线使用预算可覆盖的最大规则点阵，其叶格中心由四角推导而非额外请求；起点保留数学锚点。扇形基线采用 32 个方向、四个初始距离层，再细化最外已知可达区间，它不能可靠表达孔洞和非星形区域。

误纳和漏纳使用面积口径；未知引起的漏纳仍计入。边界 P95 取双方全部外环及孔洞边界的 5 米间隔样点距离。空边界或范围可能截断时报告 `null`。合成场景只验证算法规则与对这些时间函数的重建能力，不证明真实社区步行准确率，也不证明自适应算法普遍优于基线。

实际测试结果与失败案例见 [测试记录](TEST_REPORT.md)。

## N04/N05 参数补充

边界规则与合成接口说明见 [N04参数记录](../backend/docs/N04-算法参数与边界.md)，四组Mock与既有契约见 [N05接口契约](../backend/docs/N05-接口契约.md)。
