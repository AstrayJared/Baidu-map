from shapely.geometry import box, Polygon, mapping

from app.geo.projection import MetricProjection
from tools.hybrid_fixture import fixture
from tools.validate_hybrid import freeze, metrics


def plan_fixture(fill_size):
    result = fixture().model_dump(mode='json', by_alias=True)
    origin = (result['center']['lng'], result['center']['lat'])
    projection = MetricProjection(32651)
    x, y = projection.origin(origin)
    extent = box(x-1200,y-1200,x+1200,y+1200)
    g = box(x-400,y-400,x+400,y+400)
    fill = box(x-fill_size,y-fill_size,x+fill_size,y+fill_size) if fill_size else Polygon()
    d = dict(diagnostics=dict(metric_crs='EPSG:32651', metric_geometry=mapping(g), origin_xy=(x,y),
             metric_hard_obstacles=mapping(Polygon()), metric_inferred_fill=mapping(fill), metric_support=mapping(extent)))
    return freeze(result,d,dict(origin=origin,requests_used=0,samples=[]))


def test_geometry_adaptive_audit_preserves_100_independent_unique_points():
    small, large = plan_fixture(80), plan_fixture(280)
    assert small['allocations']['inferred_fill'] < large['allocations']['inferred_fill']
    for plan in (small,large,plan_fixture(0)):
        assert len(plan['cases']) == 100
        assert len({tuple(r['coordinate']) for r in plan['cases']}) == 100
        assert plan['allocations']['exterior'] <= 2
        assert plan['allocations']['boundary'] >= 83
        assert all('kind' not in r or r['kind'] != 'contour' for r in plan['cases'])


def test_tolerance_does_not_change_evidence_or_mix_contour_accuracy():
    plan = plan_fixture(80)
    observations = []
    for r in plan['cases']:
        duration = 500 if r['prediction'] else 1200
        observations.append(dict(request_coordinate=r['coordinate'], evidence=dict(reachable=duration<=900,duration=duration)))
    # A single classification disagreement within the user's report tolerance.
    observations[0]['evidence'] = dict(reachable=not plan['cases'][0]['prediction'],
                                      duration=910 if plan['cases'][0]['prediction'] else 890)
    measured = metrics(plan,dict(samples=observations,requests_used=100))
    assert measured['overall']['accuracy'] == 1
    assert measured['strict_overall']['accuracy'] == .99
    assert measured['overall']['within_tolerance'] == 1
    assert measured['cases'][0]['evidence'] == observations[0]['evidence']
    observations[0]['evidence']['reachable'] = None
    measured = metrics(plan,dict(samples=observations,requests_used=100))
    assert measured['overall']['api_unknown'] == 1
