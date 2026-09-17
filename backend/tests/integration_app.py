"""Offline browser harness only. Never use this app for real routing experiments."""
import asyncio
import math
from pathlib import Path

from life_circle.providers import AnalyticProvider
from life_circle.scenarios import scenarios
from app.config import Settings
from app.algorithms.hybrid_isochrone.models import Evidence, Validity
from unittest.mock import patch

settings = Settings(_env_file=None, baidu_map_ak="", analysis_provider="synthetic",
    cors_origins=["http://127.0.0.1:5178"])
# main creates its default app at import time; offline tests must not load credentials.
with patch("app.config.load_settings", return_value=settings):
    from app.main import create_app

cases = scenarios()


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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def query_walking_time(self, origin, destination, deadline):
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
    result = AnalyticProvider(origin, function)
    if origin[0] == 116.409:
        query = result.query_walking_time

        async def slow(*args):
            await asyncio.sleep(.15)
            return await query(*args)
        result.query_walking_time = slow
    return result


settings.hybrid_ledger_dir = Path(__file__).resolve().parents[1] / '.tmp/hybrid-browser'
settings.hybrid_obstacle_path = Path('missing-test-obstacles')
settings.hybrid_risk_path = Path('missing-test-risks')
app = create_app(settings, provider_factory=provider,
                 hybrid_provider_factory=lambda projection, config: HybridProvider(projection))
app.state.hybrid.gate = FastGate()
