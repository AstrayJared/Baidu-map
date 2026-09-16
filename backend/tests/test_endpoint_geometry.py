import pytest
from shapely.geometry import Polygon, box, shape
from shapely import make_valid

from life_circle.field import business_geometry as raw_geometry, GeometryError
from tools.endpoint_radial_experiment import P, local


# Reduced from the 200-call run: valid local hole folds after geographic export.
SLIVER_HOLE = [
    (453.9708738169897, 139.7570852609855),
    (454.15404836923767, 139.97441763496454),
    (454.3360563958734, 140.03051416790245),
    (435.3909863621628, 134.1914708861254),
    (437.79897050628387, 134.93363354978072),
    (453.9644742476248, 130.85955522289862),
]


def test_projection_roundoff_is_repaired_without_filling_the_real_hole():
    from tools.endpoint_geometry import business_geometry
    original = Polygon(box(0, 0, 1000, 1000).exterior.coords, [SLIVER_HOLE])
    assert original.is_valid and not shape(raw_geometry(original, P)).is_valid
    exported = business_geometry(original, P)
    assert shape(exported).is_valid
    restored = make_valid(local(exported))
    assert original.symmetric_difference(restored).area < 1e-5
    assert original.difference(restored.buffer(1e-6)).is_empty
    assert restored.difference(original.buffer(1e-6)).is_empty
    assert sum(len(p.interiors) for p in shape(exported).geoms) >= 1


def test_real_self_intersection_is_rejected_not_hidden_by_make_valid():
    from tools.endpoint_geometry import business_geometry
    with pytest.raises(GeometryError, match='local'):
        business_geometry(Polygon([(0, 0), (100, 100), (0, 100), (100, 0)]), P)


def test_valid_components_holes_and_empty_geometry_are_preserved():
    from tools.endpoint_geometry import business_geometry
    from shapely.geometry import MultiPolygon
    poly = Polygon(box(0, 0, 100, 100).exterior.coords, [box(20, 20, 40, 40).exterior.coords])
    for original in (MultiPolygon([poly, box(200, 0, 250, 50)]), MultiPolygon()):
        assert business_geometry(original, P) == raw_geometry(original, P)


@pytest.mark.parametrize('case', ['reentry-w25-a11-offset60', 'reentry-w25-a11-snap80',
                                  'narrow-w25-a0-none'])
def test_local_overlay_does_not_create_unexportable_slivers(case):
    import asyncio
    from shapely.geometry import Point
    from life_circle.models import IsochroneRequest, CancelToken
    from tools.endpoint_e83_experiment import cases
    from tools.endpoint_multicross_boundary import compute_multicross_boundary
    from tools.endpoint_radial_experiment import ORIGIN
    from tools.diagnostic_common import no_network

    async def run():
        _, _, factory = next(row for row in cases('development') if row[0] == case)
        provider = factory()
        request = IsochroneRequest(ORIGIN, 'bd09ll', budget=300, extent=1200,
            max_extent=1200, expand=False, concurrency=30)
        with no_network():
            result = await compute_multicross_boundary(request, provider, CancelToken(),
                radial_step=100, parallel_sampling=True, edge_batch_size=30)
        assert provider.calls == result['calls'] <= 300
        for key in ('geometry', 'candidateGeometry', 'unknownRegion'):
            assert shape(result[key]).is_valid
            assert local(result[key]).is_valid
        assert not any(shape(result['geometry']).covers(Point(e['route_destination']))
            for e in result['observationEvidence'] if e['accepted'] and e['observed_duration'] > 900)
    asyncio.run(run())
