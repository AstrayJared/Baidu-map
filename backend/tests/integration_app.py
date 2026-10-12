"""Offline browser harness only. Never use this app for real routing experiments."""
import asyncio
import math
from pathlib import Path

from app.algorithms.baidu_e82 import EndpointAnalyticProvider as AnalyticProvider
from life_circle.scenarios import scenarios
from app.config import Settings
from app.algorithms.hybrid_isochrone.models import Evidence, Validity
from unittest.mock import patch

settings = Settings(_env_file=None, baidu_map_ak="", analysis_provider="synthetic",
    # This harness is offline by construction, so it must not initialise -- let alone
    # spend -- the deployment's own ledger and checkup store.
    checkup_dir=Path("../.tmp/integration-app/checkups").resolve(),
    quota_ledger_path=Path("../.tmp/integration-app/quota.sqlite3").resolve(),
    cors_origins=["http://127.0.0.1:5178"])
# main creates its default app at import time; offline tests must not load credentials.
with patch("app.config.load_settings", return_value=settings):
    from app.main import create_app

cases = scenarios()
# Centre whose whole task fails after a short run, for the browser task-lifecycle
# checks. Per-query exceptions are (correctly) absorbed as unknown evidence by both
# engines, so the failure is raised where the provider is opened or closed.
FAILING_LNG = 116.412


async def fail_soon():
    await asyncio.sleep(1.5)
    raise RuntimeError("offline harness: scripted walking-service failure")


class FailingProvider:
    network = False

    async def __aenter__(self):
        await fail_soon()

    async def __aexit__(self, *args):
        pass


class FastGate:
    interval = 0
    qps = 10000

    def __init__(self):
        self.attempt_lock = asyncio.Lock()

    async def wait(self, deadline, *, cost=1):
        return True

    def completed(self, reason, *, cost=1):
        pass


class HybridProvider:
    network = False
    identity = ('synthetic-hybrid-browser',)

    def __init__(self, projection):
        self.projection = projection
        self.failing = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.failing:
            await fail_soon()

    async def query_walking_time(self, origin, destination, deadline):
        if FAILING_LNG in (origin[0], destination[0]):
            self.failing = True
            raise RuntimeError("offline harness: scripted walking-service failure")
        await asyncio.sleep(.005)
        a, b = self.projection.origin(origin), self.projection.origin(destination)
        duration = math.dist(a, b) / 1.2
        return Evidence(validity=Validity.REACHABLE if duration <= 900 else Validity.UNREACHABLE,
                        reason=None, duration=duration, returned_origin=origin, returned_destination=destination,
                        origin_offset_m=0, destination_offset_m=0)


def provider(origin):
    scene = {116.405: "hole", 116.406: "components", 116.407: "global_failure", 116.410: "local_failure"}.get(origin[0], "plane")
    function = cases[scene].observed
    if origin[0] == 116.408:
        # Unknown neighborhood isolates the known zero-time origin from support;
        # all remaining supported triangles have evidence above the threshold.
        function = lambda x, y: None if math.hypot(x, y) <= 401 else 2000
    if origin[0] == FAILING_LNG:
        return FailingProvider()
    result = AnalyticProvider(origin, function)
    if origin[0] == 116.409:
        query = result.query_walking_time

        async def slow(*args):
            await asyncio.sleep(.15)
            return await query(*args)
        result.query_walking_time = slow
    return result


settings.hybrid_ledger_dir = Path(__file__).resolve().parents[1] / '.tmp/hybrid-browser'
settings.checkup_dir = settings.hybrid_ledger_dir / 'checkups'
settings.osm_graph_cache_path = settings.hybrid_ledger_dir / 'missing-graph'
settings.osm_pbf_path = settings.hybrid_ledger_dir / 'missing.osm.pbf'
settings.hybrid_obstacle_path = Path('missing-test-obstacles')
settings.hybrid_risk_path = Path('missing-test-risks')
app = create_app(settings, provider_factory=provider,
                 hybrid_provider_factory=lambda projection, config: HybridProvider(projection))
app.state.hybrid.gate = FastGate()
app.state.checkups.offline = None
for engine in app.state.checkups.registry.engines.values():
    engine.gate = FastGate()
