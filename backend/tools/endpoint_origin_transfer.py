"""E8.2.1 离线原型：第二个道路吸附点的观测，能不能用一次实测的连接耗时归到同一参考起点。

背景（`CURRENT_STATE.md` 2026-10-10 / 提交 `2624704`）：请求中心落在两处道路吸附点之间时，
百度按**目标点**分别吸附路线起点；求解器只采纳先到的那个起点，来自另一处的那批观测被
`different_actual_origin` 拒收。扇区里因此一条证据都没有，环合不起来。

做法：对每一条来自**另一个**实际起点 B 的观测，先实测一次 A→B 的步行耗时 t。
路网上的步行时间近似满足三角不等式，所以"从 A 到目标"的真实耗时 d_A 满足

    |d_A - d_B| <= t        →        d_A ∈ [d_B - t, d_B + t]

于是可以在**不猜**的前提下给出标签：

    d_B + t < 900  → 必然可达（确定）
    d_B - t > 900  → 必然不可达（确定）
    其余           → 无法确定，保持未知

无法确定时**原样交回**，求解器照旧按 `different_actual_origin` 拒收 —— 证据保留、结论不变。

这条规则自带保护：两处吸附点隔着河或墙时，t 会因为绕行而变大，区间随之变宽，观测落回
"无法确定"，转移自动被拒绝 —— 而不是把 20 米的直线距离当成 20 米的步行距离。
`split20-barrier` 用例钉住这一点，`naive` 变体（去掉那条界）演示不设界会错成什么样。

**这是离线原型，不接生产请求路径。** 它用"改写 `route_origin`"的方式模拟"求解器接受转移证据"，
只为回答"这么做能不能让环合起来、代价多大、会不会把错误的区域算进来"。
生产实现若要做，应当是在求解器里显式接受带来源与不确定度的转移证据（新增字段与诊断），
而不是改写既有字段 —— 改写会让"这条证据是从哪起步的"这件事在证据里消失。

用法（cwd=backend）：
    .venv/bin/python -m tools.endpoint_origin_transfer            # 跑全部用例，打对照表
    .venv/bin/python -m tools.endpoint_origin_transfer --json     # 同一份结果的 JSON
"""
import argparse
import asyncio
import json
import math
import sys
from dataclasses import replace

from shapely.geometry import Point, shape
from shapely.ops import transform

from life_circle.coordinates import LocalProjection
from life_circle.models import CancelToken, RouteObservation

from app.algorithms.baidu_e82 import EndpointAnalyticProvider
from tools.endpoint_radial_experiment import ORIGIN, Synthetic, radius

#: 与求解器一致的可达阈值（秒）。
THRESHOLD_SECONDS = 900.0
#: 解析替身的速度：time = straight-line distance / 1.2 —— 本文件自建用例的真值半径因此是 1080 米。
SYNTHETIC_SPEED = 1.2
SPEED_TRUTH_RADIUS = THRESHOLD_SECONDS * SYNTHETIC_SPEED
#: 另一处吸附点的位置（局部坐标，米）：与 `2624704` 记录的 20.11 米同量级。
OTHER_SNAP_LOCAL = (-20.0, 0.0)


def transfer_interval(duration: float, connection_seconds: float) -> tuple[float, float]:
    """从 B 出发的实测耗时，换成"从 A 出发"的区间。t 是 d_A 与 d_B 之差的上界。"""
    if not math.isfinite(duration) or not math.isfinite(connection_seconds) or connection_seconds < 0:
        raise ValueError('Invalid transfer inputs')
    return duration - connection_seconds, duration + connection_seconds


def classify_transfer(duration: float, connection_seconds: float,
                      threshold: float = THRESHOLD_SECONDS) -> str:
    """`reachable` / `unreachable` / `unknown` —— 只有前两者是"确定"。

    边界与求解器一致：`duration <= 900` 判可达。所以上界**等于**阈值时仍然确定可达，
    而下界等于阈值时不能确定不可达（d_A 可能正好是 900）。
    """
    low, high = transfer_interval(duration, connection_seconds)
    if high <= threshold:
        return 'reachable'
    if low > threshold:
        return 'unreachable'
    return 'unknown'


class TransferOrigin(EndpointAnalyticProvider):
    """一个扇区的路线从另一处吸附点起步；可选地，从 B 出发要绕一次远路。

    * 不设绕行时：时长按 B 起算（`|dest - B| / 1.2`），与真实上游"按目标点吸附起点"一致；
    * 设了绕行时：B 在河/墙的另一侧，去对岸任何一点的耗时都多出 `detour_seconds`。
    """

    def __init__(self, origin, wedge=(3.4, 5.6), detour_seconds=None):
        super().__init__(origin, lambda x, y: (x * x + y * y) ** .5 / SYNTHETIC_SPEED)
        self.wedge = wedge
        self.detour_seconds = detour_seconds
        self.other = self.projection.to_geographic(OTHER_SNAP_LOCAL)

    def _is_connection_probe(self, destination) -> bool:
        x, y = self.projection.to_local(destination)
        return math.hypot(x - OTHER_SNAP_LOCAL[0], y - OTHER_SNAP_LOCAL[1]) < 1.0

    async def query_walking_time(self, origin, destination, deadline):
        if self._is_connection_probe(destination):
            # A→B 的连接实测：没有绕行时就是 20 米的步行，有绕行时是那次绕行本身。
            seconds = (self.detour_seconds if self.detour_seconds is not None
                       else math.dist((0.0, 0.0), OTHER_SNAP_LOCAL) / SYNTHETIC_SPEED)
            self.calls += 1
            return RouteObservation(destination, seconds, None, observed_duration=seconds,
                                    attempts=1, route_origin=origin, route_destination=destination,
                                    request_origin=origin, origin_offset_m=0.0,
                                    destination_offset_m=0.0)
        observation = await super().query_walking_time(origin, destination, deadline)
        x, y = self.projection.to_local(destination)
        angle = math.atan2(y, x) % (2 * math.pi)
        if not (self.wedge[0] <= angle <= self.wedge[1]):
            return observation
        if self.detour_seconds is None:
            duration = math.hypot(x - OTHER_SNAP_LOCAL[0], y - OTHER_SNAP_LOCAL[1]) / SYNTHETIC_SPEED
        else:
            duration = observation.observed_duration + self.detour_seconds
        return replace(observation, duration=duration, observed_duration=duration,
                       route_origin=self.other, origin_offset_m=20.0)


class Endpointless(EndpointAnalyticProvider):
    """上游回了时长、却一次也没回端点：没有可转移的东西。"""

    def __init__(self, origin):
        super().__init__(origin, lambda x, y: (x * x + y * y) ** .5 / SYNTHETIC_SPEED)

    async def query_walking_time(self, origin, destination, deadline):
        observation = await super().query_walking_time(origin, destination, deadline)
        return replace(observation, route_origin=None, route_destination=None,
                       endpoint_verified=False)


class TransferProvider:
    """把另一个吸附点的观测改写成"从参考起点出发"的证据 —— 只在这件事能被证明时才改。

    * 参考起点 = 第一条观测的实际起点（与求解器的先到规则一致）；
    * 另一处实际起点每次出现只实测一次连接耗时（多花一次调用），之后复用；
    * `naive=True` 时去掉那条界，无条件改写 —— 只为在实验里演示"不设界会错成什么样"。
    """

    def __init__(self, inner, *, threshold=THRESHOLD_SECONDS, naive=False):
        self.inner = inner
        self.network = inner.network
        self.identity = tuple(getattr(inner, 'identity', ())) + (
            'origin-transfer', 'naive' if naive else 'bounded')
        self.threshold = threshold
        self.naive = naive
        self.reference = None
        self.connections: dict[tuple, float | None] = {}
        self.connection_calls = 0
        self.accepted: list[dict] = []
        self.refused: list[dict] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def _connection_seconds(self, origin, other, deadline):
        """实测参考起点到另一处实际起点的步行耗时；测不到就是 None（不猜）。"""
        self.connection_calls += 1
        observation = await self.inner.query_walking_time(origin, other, deadline)
        duration = observation.observed_duration
        return float(duration) if duration is not None else None

    async def query_walking_time(self, origin, destination, deadline):
        observation = await self.inner.query_walking_time(origin, destination, deadline)
        actual = observation.route_origin
        duration = observation.observed_duration
        if actual is None or duration is None:
            # 端点缺失或没有可用时长：与转移无关，照旧交给求解器。
            return observation
        if self.reference is None:
            self.reference = (tuple(origin), tuple(actual))
            return observation
        if (tuple(origin), tuple(actual)) == self.reference:
            return observation
        if tuple(actual) not in self.connections:
            self.connections[tuple(actual)] = await self._connection_seconds(
                self.reference[1], actual, deadline)
        connection = self.connections[tuple(actual)]
        row = {'duration': duration, 'connection': connection}
        if connection is None:
            self.refused.append({**row, 'verdict': 'unmeasurable'})
            return observation
        verdict = 'reachable' if self.naive else classify_transfer(duration, connection, self.threshold)
        if verdict == 'unknown':
            self.refused.append({**row, 'verdict': verdict})
            return observation
        self.accepted.append({**row, 'verdict': verdict})
        return replace(observation, route_origin=origin, request_origin=origin)

    def summary(self) -> dict:
        return {'accepted': len(self.accepted), 'refused': len(self.refused),
                'connectionCalls': self.connection_calls,
                'connectionSeconds': [None if value is None else round(value, 2)
                                      for value in self.connections.values()],
                'verdicts': sorted({row['verdict'] for row in self.accepted + self.refused})}


def local_geometry(geometry, origin):
    projection = LocalProjection(origin)
    return transform(lambda x, y, z=None: projection.to_local((x, y)), shape(geometry))


def truth_iou(geometry, origin, truth_radius):
    """与真值圆（局部坐标，半径按各自模型的阈值算）的 IoU；没有几何时是 0。"""
    if geometry is None or truth_radius is None:
        return 0.0
    published = local_geometry(geometry, origin)
    if not published.is_valid:
        published = published.buffer(0)
    truth = Point(0.0, 0.0).buffer(truth_radius, quad_segs=64)
    union = published.union(truth).area
    return 0.0 if union <= 0 else published.intersection(truth).area / union


async def run_case(name, factory, truth_radius, *, budget=200, transfer=True, naive=False):
    from app.algorithms.baidu_e82 import compute_e82
    from app.engines.baidu_e82 import e82_request

    provider = factory()
    wrapped = TransferProvider(provider, naive=naive) if transfer else None
    result = await compute_e82(e82_request(ORIGIN, budget), wrapped or provider, CancelToken())
    payload = result.to_dict()
    diagnostics = payload['boundaryDiagnostics']
    ring = diagnostics['ringAssembly']
    geometry = payload.get('geometry')
    return {
        'case': name, 'mode': ('naive' if naive else 'bounded') if transfer else 'baseline',
        'geometry': geometry is not None,
        'quality': result.quality,
        'stopReason': result.stop_reason,
        'calls': payload['statistics']['requests'],
        'areaKm2': None if geometry is None else round(local_geometry(geometry, ORIGIN).area / 1e6, 3),
        'iouVsTruth': round(truth_iou(geometry, ORIGIN, truth_radius), 4),
        'maxGapDegrees': None if ring is None or ring['maxGapRadians'] is None
        else round(math.degrees(ring['maxGapRadians']), 1),
        'ringAssembled': None if ring is None else ring['assembled'],
        'rejected': diagnostics['observations']['rejected'],
        'rejectedReasons': diagnostics['observations']['rejectedReasons'],
        'transfers': wrapped.summary() if wrapped is not None else None,
    }


def cases():
    """case → (provider 工厂, 真值半径米)。

    真值半径按各自的模型算，不混用：`Synthetic('circle')` 的边界在 `radius('circle')`（650 米），
    本文件自建的三个用例是"耗时 = 距离 / 1.2"，边界在 900 × 1.2 = 1080 米。
    """
    return {
        # 单一起点：不该有任何转移（转移只在起点确实不同时才发生）。
        'circle': (lambda: Synthetic('circle'), radius('circle', 0.0)),
        # 真实失败形状：西南扇区另有吸附点，相距 20 米，连接就是 20 米的步行。
        'split20': (lambda: TransferOrigin(ORIGIN), SPEED_TRUTH_RADIUS),
        # 同样的 20 米，但两处吸附点在河的两岸：从 B 出发要多走 600 秒。
        'split20-barrier': (lambda: TransferOrigin(ORIGIN, detour_seconds=600.0), SPEED_TRUTH_RADIUS),
        # 上游不回端点：没有可转移的东西。
        'endpointless': (lambda: Endpointless(ORIGIN), SPEED_TRUTH_RADIUS),
    }


async def experiment():
    rows = []
    for name, (factory, truth_radius) in cases().items():
        rows.append(await run_case(name, factory, truth_radius, transfer=False))
        rows.append(await run_case(name, factory, truth_radius, transfer=True))
        if name == 'split20-barrier':
            rows.append(await run_case(name, factory, truth_radius, transfer=True, naive=True))
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    rows = asyncio.run(experiment())
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"{'case':16s} {'mode':8s} {'geom':5s} {'quality':12s} {'km2':>7s} {'IoU':6s} "
          f"{'maxGap':>7s} {'ring':5s} rejected")
    for row in rows:
        print(f"{row['case']:16s} {row['mode']:8s} {str(row['geometry']):5s} {row['quality']:12s} "
              f"{str(row['areaKm2']):>7s} {row['iouVsTruth']:<6.4f} {str(row['maxGapDegrees']):>7s} "
              f"{str(row['ringAssembled']):5s} {row['rejected']} {row['rejectedReasons']}")
        transfer = row['transfers']
        if transfer is not None and (transfer['accepted'] or transfer['refused']):
            print(f"{'':16s} └─ 转移：接受 {transfer['accepted']} 条、拒绝 {transfer['refused']} 条；"
                  f"连接调用 {transfer['connectionCalls']} 次；"
                  f"连接耗时 {transfer['connectionSeconds']} 秒；判定 {transfer['verdicts']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
