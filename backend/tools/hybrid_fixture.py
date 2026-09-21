"""Small explicitly synthetic wire fixture. Never queries any provider."""
from shapely.geometry import box

from app.algorithms.hybrid_isochrone.extent import ALGORITHM_VERSION
from app.algorithms.hybrid_isochrone.models import HybridConfig
from app.contracts import Data, Issue, Origin, Rules
from app.geo.projection import MetricProjection
from app.hybrid_api import content_hash
from app.hybrid_contracts import HybridIsochrone, HybridReadiness, HybridResultResponse
from app.test_origin import NORMALIZED_TEST_ORIGIN


def fixture():
    origin = Origin(lng=NORMALIZED_TEST_ORIGIN[0], lat=NORMALIZED_TEST_ORIGIN[1])
    projection = MetricProjection(32651)
    x, y = projection.origin(NORMALIZED_TEST_ORIGIN)
    extent = box(x-1200, y-1200, x+1200, y+1200)
    geometry = box(x-400, y-400, x+400, y+400)
    from app.algorithms.hybrid_isochrone.polygon_builder import multipolygon
    public = lambda g: projection.public_geometry(multipolygon(g))
    config = HybridConfig()
    core = HybridIsochrone(algorithm_version=ALGORITHM_VERSION, quality="partial",
        coverage_policy="continuous_land_interior", geometry=public(geometry), evidence_geometry=public(geometry),
        inferred_fill_geometry=None, unknown_region=public(extent.difference(geometry)),
        evidence_unknown_region=public(extent.difference(geometry)), computation_extent=public(extent),
        extent_truncated=False, requests_used=0, valid_baidu_samples=0, invalid_baidu_samples=0, unknown_samples=0,
        boundary_error_estimate=None, stop_reason="synthetic_contract_fixture", warnings=["synthetic_contract_fixture"],
        config=config, readiness=HybridReadiness(mode="degraded", graph_available=False,
            data_version_matches=False, coverage_available=False, origin_in_coverage=False, extent_in_coverage=False,
            obstacle_layer_available=False, risk_layer_available=False, warnings=["synthetic_contract_fixture"]),
        timing_seconds=dict.fromkeys(("preparation", "obstacle_load", "requests", "geometry_rebuilds", "compute_total", "task_total"), 0))
    return HybridResultResponse(task_id="synthetic-hybrid-fixture", task_status="completed", status="partial",
        business_status="partial", data_source="synthetic", center=origin, generated_at=1789516800,
        facilities_status="not_integrated", rules=Rules(time_confirmation="mock_only"),
        data=Data(geometry=core.geometry, unknown_region=core.unknown_region, computation_extent=core.computation_extent),
        algorithm=core, isochrone=core, config_hash=content_hash(dict(origin=origin.model_dump(), coordinate_system="bd09ll", config=config.model_dump(mode="json"))),
        result_hash=content_hash(core.model_dump(mode="json", by_alias=True)),
        warnings=[Issue(code="SYNTHETIC_CONTRACT_FIXTURE", message="Synthetic fixture; no Baidu requests.", scope="all")])
