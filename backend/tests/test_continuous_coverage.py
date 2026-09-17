import json
from types import SimpleNamespace

import pytest
from shapely.geometry import box, Polygon, MultiPolygon, Point, LineString, mapping, shape

from app.algorithms.hybrid_isochrone.coverage_builder import build_coverage
from app.algorithms.hybrid_isochrone.hard_obstacles import LocalObstacles, ObstacleIndex, load_obstacles, walkable_bridge
from app.algorithms.hybrid_isochrone.models import HybridConfig, Sample, Evidence, Validity
from app.algorithms.hybrid_isochrone.polygon_builder import BuiltPolygon, build_polygon, multipolygon
from app.geo.projection import MetricProjection
from tools.test_origin import TEST_ORIGIN


def sample(x, y, reachable=True, duration=500):
    return Sample(f'{x}/{y}', (x, y), (x, y), 'GEOMETRIC', 'test', 0,
                  Evidence(Validity.UNKNOWN if reachable is None else Validity.REACHABLE if reachable else Validity.UNREACHABLE,
                           duration=duration), {})


def built(g, extent=None):
    extent = extent if extent is not None else box(-500, -500, 500, 500)
    g = multipolygon(g)
    return BuiltPolygon(g, g, multipolygon(extent.difference(g)), extent, [], [])


@pytest.mark.parametrize('radius', [2, 50, 200])
def test_all_non_water_holes_filled_without_relabelling(radius):
    outer = box(-300, -300, 300, 300)
    g = Polygon(outer.exterior.coords, [box(-radius, -radius, radius, radius).exterior.coords])
    rows = [sample(0, 0, False, 1300), sample(1, 1, None)]
    cov = build_coverage(built(g), rows, HybridConfig(), LocalObstacles())
    assert cov.geometry.equals(outer)
    assert len(cov.conflicts) == 1 and cov.conflicts[0]['duration_s'] == 1300
    assert cov.diagnostics()['fill_false_positive_count'] == 1
    assert cov.diagnostics()['fill_sample_stats']['unreachable'] == 1
    assert cov.diagnostics()['fill_sample_stats']['unknown'] == 1
    assert rows[0].evidence.reachable is False and rows[1].evidence.reachable is None
    assert cov.diagnostics()['non_obstacle_interior_residual_m2'] == 0


def test_water_and_land_island_preserved():
    water = Polygon(box(-100, -100, 100, 100).exterior.coords,
                    [box(-10, -10, 10, 10).exterior.coords])
    c = build_coverage(built(box(-200, -200, 200, 200)), [], HybridConfig(), LocalObstacles(water))
    assert not c.geometry.covers(Point(50, 50))
    assert c.geometry.covers(Point(0, 0))
    assert c.geometry.intersection(water).area == 0


@pytest.mark.parametrize('extra,expected', [({}, True), ({'foot': 'no'}, False),
    ({'access': 'private'}, False), ({'tunnel': 'yes'}, False), ({'highway': 'motorway'}, False)])
def test_bridge_requires_permission_and_keeps_water_outside_corridor(extra, expected):
    water = box(-20, -100, 20, 100)
    bridge = LineString([(-50, 0), (50, 0)])
    tags = dict(bridge='yes', highway='footway', **{'osm_id': 1})
    tags.update(extra)
    c = build_coverage(built(box(-100, -100, 100, 100)), [sample(-50, 0), sample(50, 0)],
                       HybridConfig(), LocalObstacles(water, [(bridge, tags)]))
    assert c.geometry.covers(Point(0, 0)) == expected
    assert not c.geometry.covers(Point(0, 5))


def test_bridge_without_evidence_or_with_wet_endpoint_cannot_open_water():
    water = box(-20, -100, 20, 100)
    for bridge, rows in [(LineString([(-50, 0), (50, 0)]), []),
                         (LineString([(-50, 0), (50, 0)]), [sample(-50, 0)]),
                         (LineString([(-50, 0), (0, 0)]), [sample(-50, 0), sample(50, 0)])]:
        c = build_coverage(built(box(-100, -100, 100, 100)), rows, HybridConfig(),
                          LocalObstacles(water, [(bridge, dict(bridge='yes', highway='footway'))]))
        assert not c.geometry.covers(Point(0, 0))


def test_bridge_does_not_expand_existing_far_bank_coverage():
    water = box(-20, -100, 20, 100)
    c = build_coverage(built(box(-100, -100, 0, 100)), [sample(-50, 0), sample(50, 0)], HybridConfig(),
                      LocalObstacles(water, [(LineString([(-50, 0), (50, 0)]), dict(bridge='yes', highway='footway'))]))
    assert not c.geometry.covers(Point(30, 0))


def test_real_time_notch_not_filled_and_islands_not_joined():
    g = box(-200, -200, 200, 200).difference(box(-20, 0, 20, 210))
    b = built(g)
    # Supported negative region, not an unknown triangular wedge.
    b.support = multipolygon(box(-250, -250, 250, 250))
    b.unknown = multipolygon(b.extent.difference(b.support))
    c = build_coverage(b, [sample(0, 50, False, 1500)], HybridConfig(), LocalObstacles())
    assert not c.geometry.covers(Point(0, 50))
    islands = MultiPolygon([box(-200, -100, -10, 100), box(10, -100, 200, 100)])
    c = build_coverage(built(islands), [], HybridConfig(), LocalObstacles())
    assert len(c.geometry.geoms) == 2


def test_unknown_inward_notch_is_repaired_and_reported_separately():
    g = box(-200, -200, 200, 200).difference(box(-20, 0, 20, 210))
    b = built(g)
    c = build_coverage(b, [sample(-30, 20), sample(30, 20)], HybridConfig(), LocalObstacles())
    assert c.geometry.covers(Point(0, 20))
    assert any(r['source'] == 'inward_notch_repair' for r in c.repairs)


def test_unknown_vertex_gap_gets_local_witness_fill_not_global_envelope():
    # One invalid vertex breaks an otherwise all-positive local land surface.
    rows = [sample(x, y) for x, y in [(-150, -150), (150, -150), (150, 150), (-150, 150), (0, 0)]]
    rows += [sample(80, 80, None)]
    b = build_polygon(rows, (0, 0), HybridConfig())
    c = build_coverage(b, rows, HybridConfig(), LocalObstacles())
    assert c.geometry.area >= b.geometry.area
    assert not c.geometry.covers(Point(400, 0))
    assert rows[-1].evidence.reachable is None


def test_missing_obstacles_explicit_and_wrong_version_rejected(tmp_path):
    projection = MetricProjection(32651)
    assert load_obstacles(tmp_path/'missing', projection, 'v', box(0, 0, 100, 100)).warnings
    with pytest.raises(ValueError, match='version'):
        ObstacleIndex(dict(schema_version=1, coordinate_system='wgs84', osm_data_version='old', features=[]), projection, 'new')


def test_line_water_without_width_stays_unresolved_not_arbitrarily_buffered():
    p = MetricProjection(32651)
    payload = dict(schema_version=1, coordinate_system='wgs84', osm_data_version='v', features=[
        dict(geometry=mapping(LineString([(121.5, 31.2), (121.5001, 31.2)])), properties=dict(kind='water', osm_id=1))])
    index = ObstacleIndex(payload, p, 'v')
    x, y = p.forward.transform(121.5, 31.2)
    local = index.local(box(x-100, y-100, x+100, y+100))
    assert local.water.is_empty and len(local.unresolved) == 1


def test_projection_roundtrip_and_output_hole_semantics():
    p = MetricProjection(32651)
    x, y = p.origin(TEST_ORIGIN)
    g = box(x-100, y-100, x+100, y+100).difference(box(x-20, y-20, x+20, y+20))
    public = p.public_geometry(g)
    assert shape(public).is_valid
    assert len(shape(public).interiors) == 1


def test_real_coordinate_roundoff_sliver_does_not_break_public_polygon():
    p = MetricProjection(32651)
    sliver = Polygon([(357130.9532737177, 3464534.0228066402),
                      (357132.68608662195, 3464536.584468699),
                      (357130.9532737178, 3464534.0228066402)])
    assert sliver.is_valid
    with pytest.raises(ValueError, match='public_geometry_invalid'):
        p.public_geometry(sliver)
    repaired = shape(p.public_geometry(sliver, repair_roundoff=True))
    assert repaired.is_valid and repaired.geom_type in ('Polygon', 'MultiPolygon')
    assert repaired.area == 0
    invalid = Polygon([(0,0),(2,2),(2,0),(0,2)])
    with pytest.raises(ValueError, match='invalid_source_geometry'):
        p.public_geometry(invalid, repair_roundoff=True)
