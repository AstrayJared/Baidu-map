import asyncio
import math
from dataclasses import replace

import pytest
from shapely.geometry import shape

from app.algorithms.baidu_e82 import compute_e82, EndpointAnalyticProvider
from app.engines.baidu_e82 import e82_request
from life_circle.models import CancelToken, IsochroneRequest, RouteObservation
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.endpoint_radial_experiment import ORIGIN, Synthetic


@pytest.mark.parametrize('case,budget', [('circle', 200), ('concave', 400),
    ('reentry_diagnostic', 400), ('snap80', 200), ('missing', 200), ('circle', 20)])
def test_adapter_preserves_team_geometry_evidence_and_budget(case, budget):
    async def run():
        request = IsochroneRequest(ORIGIN, 'bd09ll', budget=budget,
            extent=1200, max_extent=1200, expand=False, time_bands=(15,))
        direct = await compute_multicross_boundary(request, Synthetic(case), CancelToken())
        progress = []
        adapted = await compute_e82(request, Synthetic(case), CancelToken(), on_progress=progress.append)
        assert adapted.geometry == direct['geometry']
        if direct.get('unknownRegion') is not None:
            assert adapted.unknown_region == direct['unknownRegion']
        assert adapted.statistics.requests == direct['calls'] <= budget
        assert adapted.stop_reason == direct['completion']['reason']
        assert adapted.metadata['evidence']['negativeEvidence'] == direct['negativeEvidence']
        assert [
            (o.destination, o.duration, o.route_destination) for o in adapted.sample_observations] == [
            (o.destination, o.duration, o.route_destination) for o in direct['_observations']]
        assert progress[-1].requests == adapted.statistics.requests
        assert all(p.requests <= budget for p in progress)
        assert adapted.to_dict()['algorithm'] == 'local-multicross-e82'
        assert [b['minutes'] for b in adapted.time_bands] == [15]
    asyncio.run(run())


def test_configured_slow_qps_does_not_require_spending_full_budget_before_deadline():
    from app.analyses import AnalysisManager, AnalysisInput
    from app.config import Settings
    async def run():
        manager = AnalysisManager(Settings(_env_file=None, baidu_map_ak='configured',
            analysis_provider='baidu', analysis_qps=1))
        job = manager.create(AnalysisInput(center=dict(lng=ORIGIN[0], lat=ORIGIN[1]),
            coordinateSystem='bd09ll', budget=800, clientRequestId='low-qps'))
        # Cancel before scheduling any transport: the check concerns task admission.
        manager.cancel(job)
        await manager.close()
        assert job.status == 'cancelled'
    asyncio.run(run())


def test_synthetic_arbitrary_origin_has_exact_endpoints_without_altering_real_evidence():
    from app.config import Settings
    from app.main import create_app
    app = create_app(Settings(_env_file=None, analysis_provider='synthetic'))
    # Both entries keep separate task registries but must share one Baidu gate.
    assert app.state.analyses.gate is app.state.hybrid.gate
    assert app.state.analyses.jobs is not app.state.hybrid.jobs


def test_adapted_synthetic_origin_keeps_endpoint_evidence_exact():
    async def run():
        origin = (116.404, 39.915)
        request = IsochroneRequest(origin, 'bd09ll', budget=200)
        result = await compute_e82(request, EndpointAnalyticProvider(origin,
            lambda x, y: (x*x + y*y)**.5 / 1.2), CancelToken())
        assert result.geometry and shape(result.geometry).is_valid
        assert all(o.route_origin == origin for o in result.sample_observations)
        class MissingEndpoints:
            network = False
            identity = ('missing-endpoints',)
            async def query_walking_time(self, origin, destination, deadline):
                return RouteObservation(destination, 100)
        rejected = await compute_e82(request, MissingEndpoints(), CancelToken())
        assert rejected.quality == 'insufficient'
        assert all(o.route_destination is None for o in rejected.sample_observations)
    asyncio.run(run())


class Endpointless(EndpointAnalyticProvider):
    """上游回了时长，却一次也没有回端点：求解器一条观测都用不上。"""

    def __init__(self, origin):
        super().__init__(origin, lambda x, y: (x * x + y * y) ** .5 / 1.2)

    async def query_walking_time(self, origin, destination, deadline):
        observation = await super().query_walking_time(origin, destination, deadline)
        return replace(observation, route_origin=None, route_destination=None,
                       endpoint_verified=False)


class DriftingOrigin(EndpointAnalyticProvider):
    """每次回的实际起点都差一点：只读传输计数，这一趟看起来完全正常。"""

    def __init__(self, origin):
        super().__init__(origin, lambda x, y: (x * x + y * y) ** .5 / 1.2)

    async def query_walking_time(self, origin, destination, deadline):
        observation = await super().query_walking_time(origin, destination, deadline)
        drift = 0.04 * self.calls
        return replace(observation, route_origin=self.projection.to_geographic((drift, 0)),
                       origin_offset_m=drift)


def test_boundary_diagnostics_name_why_the_observations_were_rejected():
    """没有圈面时，修订里的诊断要说清哪些观测被拒、各自什么原因。

    两次真实体检（2026-10-10）就是这个形状：成圈花掉全部 200 次、返回
    ``geometry: null``、公开统计里只有 ``endpoint_offset`` 这一项。从那份修订看不出
    "上游没回端点"、"每次回的实际起点不一样"和"边界没收敛"的区别，只能看到一句
    ``budget``。这里把三种形态各自的诊断钉住：诊断只数求解器已经记下的东西，
    它不改判定，但没有它就没有可查的现场。
    """
    async def run():
        request = IsochroneRequest(ORIGIN, 'bd09ll', budget=200, extent=1200,
                                   max_extent=1200, expand=False, time_bands=(15,))
        adapted = await compute_e82(request, Synthetic('circle'), CancelToken())
        diagnostics = adapted.to_dict()['boundaryDiagnostics']
        observations = diagnostics['observations']
        # 立住了的那一趟：每一条观测都被采纳，实际起点只有一个，十六个方向都定住了。
        assert adapted.geometry is not None
        assert observations['logged'] == observations['accepted'] > 0
        assert observations['rejected'] == 0 and observations['rejectedReasons'] == {}
        assert observations['actualOriginGroups'] == 1
        assert observations['kinds'] == {'direction_search': 48, 'boundary_midpoint': 64}
        assert sum(observations['kinds'].values()) == observations['logged']
        assert diagnostics['directions'] == {'total': 16, 'byStatus': {'localized': 16},
                                            # 定住的方向没有原因要报，所以这个桶是 none。
                                            'byReason': {'none': 16}}
        assert diagnostics['completion']['reason'] == adapted.stop_reason
        assert diagnostics['completion']['unresolvedEdges'] == 0
        # 旧 refinement 路径不算环面装配，所以这一项缺失（None），不是"合起来了"。
        assert diagnostics['ringAssembly'] is None

        endpointless = await compute_e82(request, Endpointless(ORIGIN), CancelToken())
        assert endpointless.geometry is None and endpointless.quality == 'insufficient'
        diagnostics = endpointless.to_dict()['boundaryDiagnostics']
        observations = diagnostics['observations']
        # 一条也没采纳：原因写在名字上，不留给读者去猜。
        assert observations['accepted'] == 0
        assert observations['logged'] == observations['rejected'] > 0
        assert observations['rejectedReasons'] == {'missing_endpoints': observations['logged']}
        assert observations['actualOriginGroups'] == 0
        # 十六个方向全部止于同一个原因：这就是"额度没花完、圈面却是空的"现场缺的那句话。
        assert diagnostics['directions']['total'] == 16
        assert diagnostics['directions']['byReason'] == {'missing_endpoints': 16}

        drifting = await compute_e82(request, DriftingOrigin(ORIGIN), CancelToken())
        assert drifting.geometry is None and drifting.quality == 'insufficient'
        observations = drifting.to_dict()['boundaryDiagnostics']['observations']
        # 求解器只采纳第一个实际起点。其余每一次都带回了一个可用时长，所以传输计数
        # 一个失败都没有 —— 那正是"每次都成功、圈面却立不起来"的现场。
        assert observations['accepted'] == 1
        assert observations['rejectedReasons'] == {
            'different_actual_origin': observations['logged'] - 1}
        assert observations['actualOriginGroups'] == observations['logged'] > 1
    asyncio.run(run())


class SplitOrigin(EndpointAnalyticProvider):
    """一个扇区的路线从另一处道路吸附点起步。

    这是两次真实体检（2026-10-10 10:27 与 13:39，中心 31.299213,121.50847）的形状：请求
    中心落在两处道路吸附点之间，百度按**目标点**分别吸附 —— 193 次回同一个点、南向约
    115° 扇区里的 7 次回另一个点（两点相距 20.11 米），而求解器只采纳先到的那个起点，
    于是那 6 个方向一条证据都没有。传输计数看不出任何问题：每一次都回来了时长。
    """

    def __init__(self, origin, wedge=(3.4, 5.6)):
        super().__init__(origin, lambda x, y: (x * x + y * y) ** .5 / 1.2)
        self.wedge = wedge

    async def query_walking_time(self, origin, destination, deadline):
        observation = await super().query_walking_time(origin, destination, deadline)
        angle = math.atan2(*reversed(self.projection.to_local(destination))) % (2 * math.pi)
        if self.wedge[0] <= angle <= self.wedge[1]:
            return replace(observation, route_origin=self.projection.to_geographic((-20, 0)),
                           origin_offset_m=20)
        return observation


def test_the_ring_assembly_says_the_boundary_closed_and_at_what_gap():
    """环合起来了：具名说它合起来了，且最大角隙在那条"不能凭空连过去"的界线之内。

    同一趟里"量过的 0"与"没量过的 null"必须分得开：这一趟量过未决边，所以它是 0。
    """
    async def run():
        adapted = await compute_e82(e82_request(ORIGIN, 200), Synthetic('circle'), CancelToken())
        diagnostics = adapted.to_dict()['boundaryDiagnostics']
        ring = diagnostics['ringAssembly']
        assert adapted.geometry is not None
        assert ring['assembled'] is True and ring['reason'] is None
        assert ring['maxGapRadians'] < math.pi / 2
        # 参与合环的就是那些定住的方向，不多不少。
        assert ring['localizedNodes'] == diagnostics['directions']['byStatus']['localized']
        assert diagnostics['completion']['unresolvedEdges'] == 0
        assert adapted.statistics.unfinished_boundary == 0
    asyncio.run(run())


def test_a_wedge_from_another_road_point_names_the_ring_that_could_not_close():
    """真实失败形状：起点按目标点分裂，环因此合不拢 —— 而现在这句话写在修订里。

    在此之前，这样一条修订只剩一个 ``budget``：200 次全部花光、``geometry: null``，而
    "已定的方向之间隔着 2.7 弧度"这句求解器算过就扔了；未决边数还被写死成 0，读起来像
    "边界都完成了"。这里把两件事一起钉住。
    """
    async def run():
        adapted = await compute_e82(e82_request(ORIGIN, 200), SplitOrigin(ORIGIN), CancelToken())
        assert adapted.geometry is None and adapted.quality == 'insufficient'
        diagnostics = adapted.to_dict()['boundaryDiagnostics']
        observations, directions = diagnostics['observations'], diagnostics['directions']
        # 额度花光，而每一条观测都带回了时长：只看传输计数，这一趟没有任何异常。
        assert observations['logged'] == adapted.statistics.requests == 200
        # 两个实际起点；被拒的每一条都是因为起点对不上，而那批拒收正好落在扇区的方向上。
        assert observations['actualOriginGroups'] == 2 and observations['rejected'] > 0
        assert observations['rejectedReasons'] == {
            'different_actual_origin': observations['rejected']}
        assert directions['byReason']['different_actual_origin'] \
            == directions['byStatus'].get('unknown', 0) > 0
        # 环合不拢的具名原因，连同那个角隙本身。
        ring = diagnostics['ringAssembly']
        assert ring['assembled'] is False and ring['reason'] == 'angular_gap_too_large'
        assert ring['maxGapRadians'] >= math.pi / 2
        assert 3 <= ring['localizedNodes'] == directions['byStatus']['localized']
        # 没有环面就没有补丁窗口，这两个数因此没有被量过：null，不是 0。
        assert diagnostics['completion']['unresolvedEdges'] is None
        assert diagnostics['completion']['pendingUnattemptedEdges'] is None
        assert adapted.statistics.unfinished_boundary is None
    asyncio.run(run())
