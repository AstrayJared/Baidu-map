"""Offline browser harness only. Never use this app for real routing experiments."""
import asyncio
import math

from life_circle.providers import AnalyticProvider
from life_circle.scenarios import scenarios
from app.config import Settings
from unittest.mock import patch

settings = Settings(_env_file=None, baidu_map_ak="", analysis_provider="synthetic",
    cors_origins=["http://127.0.0.1:5178"])
# main creates its default app at import time; offline tests must not load credentials.
with patch("app.config.load_settings", return_value=settings):
    from app.main import create_app

cases = scenarios()


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


app = create_app(settings, provider_factory=provider)
