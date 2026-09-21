import asyncio

import pytest
from shapely.geometry import shape

from app.algorithms.baidu_e82 import compute_e82, EndpointAnalyticProvider
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
