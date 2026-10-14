"""Field-reviewed water corrections: scoped, versioned, and never a uniform shift.

Each test pins a place where the opposite implementation would draw a wrong map:

* a measured width applies only along the stretch that was measured;
* a review written for another OSM extract is refused and reported, not applied;
* a "sources disagree" area is neither water nor land -- fills refuse it and the
  assessment reports it as a data conflict, never as coverage or a gap;
* a reviewed bridge the measured width outgrows still crosses the river;
* revisions frozen before reviews existed keep their digest;
* a recompute after a correction asks nothing again, spends nothing, leaves the
  old revisions alone and refuses when the paid retrieval no longer fits.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from life_circle.coordinates import LocalProjection
from shapely.geometry import LineString, Point, box, mapping
from shapely.ops import transform

from app.algorithms.hybrid_isochrone.hard_obstacles import (LocalObstacles, ObstacleIndex,
                                                            extend_to_dry, load_obstacles)
from app.algorithms.hybrid_isochrone.models import Evidence, Sample, Validity
from app.algorithms.hybrid_isochrone.water_review import review_catalog
from app.checkups import CheckupError
from app.checkups.facilities import stale_for
from app.checkups.manager import _analysis_document, _data_versions
from app.checkups.models import VerificationEvidence
from app.checkups.reporting_stage import _data_sources
from app.checkups.verification_stage import _unresolved_note, carried_over
from app.checkups.water_data import water_evidence
from app.geo.projection import MetricProjection

PROJECTION = MetricProjection(32651)
OSM = dict(osm_data_version='osm-test', source_pbf_sha256='abc')
X0, Y0 = PROJECTION.forward.transform(121.5, 31.2)


def wgs(geometry):
    return mapping(transform(PROJECTION.inverse.transform, geometry))


def local_box(x0, y0, x1, y1):
    return box(X0 + x0, Y0 + y0, X0 + x1, Y0 + y1)


def write_layer(tmp_path):
    """A 400 m east-west river line without a width tag and a 16 m footbridge over it."""
    river = LineString([(X0, Y0), (X0 + 400, Y0)])
    bridge = LineString([(X0 + 200, Y0 - 8), (X0 + 200, Y0 + 8)])
    payload = dict(schema_version=1, coordinate_system='wgs84', **OSM, features=[
        dict(geometry=wgs(river), properties=dict(kind='water', osm_id=1, waterway='river')),
        dict(geometry=wgs(bridge), properties=dict(kind='bridge', osm_id=9, bridge='yes', highway='footway'))])
    path = tmp_path / 'obstacles.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


def write_review(directory, **overrides):
    review = dict(
        schemaVersion=1, reviewId='test-reach', version='2026-09-28.1', title='测试复核',
        coordinateSystem='wgs84', appliesTo=dict(osmDataVersion='osm-test', sourcePbfSha256='abc'),
        # The review checked only the western half; the width was measured along 0..300 m.
        extent=wgs(local_box(-50, -100, 200, 100)),
        reaches=[dict(osmId=1, name='测试河', widthM=20, status='confirmed',
                      corridor=wgs(local_box(0, -60, 300, 60)))],
        crossings=[dict(osmId=9, status='confirmed', anchor=[121.5, 31.2],
                        geometry=wgs(LineString([(X0 + 200, Y0 - 8), (X0 + 200, Y0 + 8)])))],
        supplements=[dict(id='pond', status='confirmed', areaM2=400, anchor=[121.5, 31.2],
                          geometry=wgs(local_box(0, 40, 20, 60)))],
        conflicts=[dict(id='maybe-pond', status='unverified', areaM2=400, anchor=[121.5, 31.2],
                        geometry=wgs(local_box(100, 40, 120, 60)))],
        basemapMisdrawn=[dict(id='misdrawn-1', kind='river_displaced', verifiedAs='land', areaM2=1000,
                              anchor=[121.5, 31.2], geometry=wgs(local_box(0, 70, 100, 80)))])
    review.update(overrides)
    directory.mkdir(exist_ok=True)
    (directory / 'review.json').write_text(json.dumps(review, ensure_ascii=False), encoding='utf-8')
    return directory


def load(tmp_path, reviews=None):
    return load_obstacles(write_layer(tmp_path), PROJECTION, 'osm-test', local_box(-100, -150, 500, 150), reviews)


def test_measured_width_applies_only_along_the_measured_stretch(tmp_path):
    plain = load(tmp_path)
    assert [u['osm_id'] for u in plain.unresolved] == [1]
    reviewed = load(tmp_path, write_review(tmp_path / 'reviews'))
    # 0..300 m takes the measured width; the last 100 m keeps the OSM state (no tag: unresolved).
    assert [u['osm_id'] for u in reviewed.unresolved] == [1]
    assert abs(reviewed.unresolved[0]['uncovered_length_m'] - 100) < 1
    [(label, osm_id, width, surface)] = reviewed.reach_items
    assert (label, osm_id, width) == ('test-reach@2026-09-28.1', 1, 20.0)
    # Round caps at both ends of the measured stretch.
    assert abs(surface.area - (300 * 20 + 3.1416 * 10 ** 2)) < 5
    assert reviewed.source['water_reviews'] == ['test-reach@2026-09-28.1']
    # The confirmed pond joins the water; the conflict does not, and is uncertain instead.
    assert reviewed.water.contains(local_box(5, 45, 15, 55))
    assert not reviewed.water.intersects(local_box(105, 45, 115, 55))
    assert abs(reviewed.conflicts.area - 400) < 1
    assert any(g.intersects(local_box(105, 45, 115, 55)) for g in reviewed.uncertain)


def test_a_review_for_another_extract_is_refused_and_reported(tmp_path):
    directory = write_review(tmp_path / 'reviews', appliesTo=dict(osmDataVersion='older', sourcePbfSha256='abc'))
    local = load(tmp_path, directory)
    assert local.reach_items == [] and local.conflicts.is_empty
    assert 'water_review_not_applied' in local.warnings
    assert local.source['water_reviews_rejected'] == ['review.json']
    assert review_catalog(directory)[0]['osmDataVersion'] == 'older'


def sample(x, y):
    return Sample(f'{x}/{y}', (x, y), (x, y), 'GEOMETRIC', 'test', 0,
                  Evidence(Validity.REACHABLE, duration=300), {})


def test_a_reviewed_bridge_spans_the_measured_channel(tmp_path):
    local = load(tmp_path, write_review(tmp_path / 'reviews'))
    [(line, tags)] = local.bridges
    # The 16 m OSM segment ends inside the 20 m measured channel.
    assert any(local.water.contains(Point(c)) for c in line.coords)
    extended = extend_to_dry(line, local.water)
    assert 20 < extended.length <= 16 + 2 * 10
    samples = [sample(X0 + 200, Y0 - 30), sample(X0 + 200, Y0 + 30)]
    _mask, corridors, records = local.mask(samples, local_box(-100, -150, 500, 150))
    assert [r['osm_id'] for r in records] == [9]
    assert records[0]['span_source'] == 'water_review'
    assert corridors.area > 0
    # Without the review the same bridge is a partial segment and not a crossing.
    unreviewed = LocalObstacles(water=local.water, bridges=local.bridges)
    assert unreviewed.mask(samples, local_box(-100, -150, 500, 150))[2] == []


def test_water_evidence_names_sources_scope_and_conflicts(tmp_path):
    local = load(tmp_path, write_review(tmp_path / 'reviews'))
    domain = local_box(0, -100, 400, 100)
    evidence = water_evidence(local, domain, PROJECTION)
    assert evidence.osm_data_version == 'osm-test' and evidence.obstacle_layer_available
    [review] = evidence.reviews
    assert review['label'] == 'test-reach@2026-09-28.1'
    assert review['extent']['coordinateSystem'] == 'bd09ll'
    assert [c['id'] for c in review['conflicts']] == ['maybe-pond']
    assert [m['id'] for m in review['basemapMisdrawn']] == ['misdrawn-1']
    assert abs(evidence.reviewed_area_m2 - 200 * 200) < 1
    assert abs(evidence.unreviewed_area_m2 - 200 * 200) < 1
    assert abs(evidence.conflict_area_m2 - 400) < 1
    text = ''.join(evidence.statements)
    assert '数据冲突／未知' in text and '未经复核' in text and '不参与计算' in text
    sources = _data_sources(evidence.model_dump(mode='json', by_alias=True))['water']
    assert sources['reviews'][0]['label'] == 'test-reach@2026-09-28.1'
    assert 'geometry' not in json.dumps(sources)


def test_revisions_without_water_evidence_keep_their_digest():
    frozen = SimpleNamespace(model_dump=lambda **_: {})
    assert 'water' not in _analysis_document({'accessibility': frozen})
    assert _analysis_document({'accessibility': frozen, 'water': frozen})['water'] == {}
    assert _data_versions('osm', 'engine', {}) == {'osm': 'osm', 'engine': 'engine'}


# -- recompute after a correction --------------------------------------------

def test_a_retrieval_stands_only_while_every_record_stays_on_its_side():
    origin = (121.5, 31.2)
    local = LocalProjection(origin)

    def square(half):
        ring = [list(local.to_geographic(xy))
                for xy in ((-half, -half), (half, -half), (half, half), (-half, half), (-half, -half))]
        return {'type': 'Polygon', 'coordinates': [ring]}

    lng, lat = local.to_geographic((450, 0))
    group = SimpleNamespace(facilities=[{'id': 'a', 'location': {'lng': lng, 'lat': lat},
                                         'observations': [{'location': {'lng': lng, 'lat': lat}}]}],
                            review_candidates=[], excluded_candidates=[])
    assert stale_for(group, square(500), square(500), origin) is None
    assert stale_for(group, square(500), square(480), origin) is None
    # Growth could take in a record the run set aside as outside the boundary.
    assert stale_for(group, square(480), square(500), origin) == 'boundary_grew'
    # A shrink that leaves a counted facility outside would count it wrongly.
    assert stale_for(group, square(500), square(400), origin) == 'counted_facility_left_boundary'


def test_carried_routes_keep_their_facts_and_reread_the_entrance_layer():
    route = {'facilityId': 'a', 'routeDistanceM': 812.0, 'withinRule': True,
             'entranceStatus': 'resolved', 'entranceOffsetM': 4.0}
    evidence = VerificationEvidence(status='partial', provider='test', checked=1, facilities=[route],
                                    notes=['核验只对本次检索到的设施成立，不构成目录完整性证明。'])
    outcome = carried_over(evidence, entrances={'a': SimpleNamespace(status='unresolved', attachment=None)},
                           revision=5)
    [record] = outcome.evidence.facilities
    assert (record['routeDistanceM'], record['withinRule']) == (812.0, True)
    assert (record['entranceStatus'], record['entranceOffsetM']) == ('unresolved', None)
    assert outcome.evidence.unresolved == 1 and outcome.status == 'partial'
    assert _unresolved_note(1) in outcome.evidence.notes
    assert '第 5 版' in outcome.evidence.notes[-1] and '未重新请求' in outcome.evidence.notes[-1]
    assert outcome.network_requests == 0


def report_review(directory, version, side):
    """A review of the report fixture's area: one unresolved conflict, no widths."""
    from test_checkup_report import PROJECTION as RP, X0 as RX, Y0 as RY

    def geo(geometry):
        return mapping(transform(RP.inverse.transform, geometry))

    directory.mkdir(exist_ok=True)
    (directory / 'review.json').write_text(json.dumps(dict(
        schemaVersion=1, reviewId='fixture', version='1', title='测试复核', coordinateSystem='wgs84',
        appliesTo=dict(osmDataVersion=version, sourcePbfSha256=None),
        extent=geo(box(RX - 1500, RY - 1500, RX + 1500, RY + 1500)),
        conflicts=[dict(id='maybe-pond', status='unverified', areaM2=side ** 2,
                        geometry=geo(box(RX + 250, RY + 250, RX + 250 + side, RY + 250 + side)))]),
        ensure_ascii=False), encoding='utf-8')


def revision_files(manager, task_id):
    return {p.name: p.read_bytes() for p in (manager.store.root / 'tasks' / task_id).glob('revision-*.json')}


def test_a_recompute_republishes_from_paid_evidence_without_asking_again(tmp_path):
    from test_checkup_facilities import ORIGIN, body, document, offset, run
    from test_checkup_report import VERSION, WATERSIDE, make_report_app
    water = tmp_path / 'obstacles.json'
    water.write_text(json.dumps({
        'schema_version': 1, 'coordinate_system': 'wgs84', 'osm_data_version': VERSION,
        'features': [{'type': 'Feature', 'properties': {'kind': 'water', 'osm_id': 1},
                      'geometry': {'type': 'Polygon', 'coordinates': [WATERSIDE + [WATERSIDE[0]]]}}]}),
        encoding='utf-8')
    reviews = tmp_path / 'reviews'
    reviews.mkdir()
    app = make_report_app(tmp_path, [ORIGIN, offset(700)], hybrid_obstacle_path=water,
                          water_review_dir=reviews)
    manager = app.state.checkups
    places, routes = manager.place_factory(None), manager.route_factory(None)
    with TestClient(app) as client:
        task_id, view = run(client, body())
        before = document(client, task_id)
    assert view['revision'] == 5 and before['water']['reviews'] == []
    sent = (len(places.sent), len(routes.sent))
    frozen = revision_files(manager, task_id)

    report_review(reviews, VERSION, 60)
    assert asyncio.run(manager.recompute(task_id, reason='fixture@1')) == [6, 7]

    # Nothing was asked again, nothing was spent, nothing published was touched.
    assert (len(places.sent), len(routes.sent)) == sent
    record = manager.store.get(task_id)
    assert (record.requests, record.network_requests, record.stage) == \
        (view['requests'], view['networkRequests'], 'ready')
    assert {k: v for k, v in revision_files(manager, task_id).items() if k in frozen} == frozen
    revisions = manager.store.revisions(task_id)
    assert [r['stage'] for r in revisions[5:]] == ['verification', 'reporting']
    after = manager.store.revision(task_id)['snapshot']
    assert after['trace']['recomputed'] == {
        'fromRevision': 5, 'reason': 'fixture@1', 'networkRequests': 0, 'boundary': 'unchanged',
        'carriedOver': ['facilities', 'verificationRoutes'],
        'previousIsochroneHash': before['trace']['isochroneHash'],
        'previousResultHash': before['trace']['resultHash']}
    assert after['trace']['budgets'] == before['trace']['budgets']
    assert after['trace']['isochroneHash'] == before['trace']['isochroneHash']
    assert after['trace']['dataVersions']['waterReviews'] == ['fixture@1']
    # The report still summarizes the revision before it.
    assert after['report']['evidence']['sourceResultHash'] == revisions[5]['result_hash']
    # The correction reaches the assessment: the conflict is unknown, never coverage.
    assert after['water']['conflictAreaM2'] > 0
    reasons = {p.get('reason') for points in after['heatmap']['categories'].values() for p in points}
    assert 'water_data_conflict' in reasons
    assert after['report']['dataSources']['water']['reviews'][0]['label'] == 'fixture@1'
    assert [f['routeDistanceM'] for f in after['verification']['facilities']] == \
        [f['routeDistanceM'] for f in before['verification']['facilities']]


def test_a_replayed_boundary_without_a_data_change_is_the_paid_one(tmp_path):
    from test_checkup_facilities import SyntheticPlaces, at_origin, body, make_app, run
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    manager = app.state.checkups
    with TestClient(app) as client:
        task_id, view = run(client, body(engine='osm_hybrid', isochrone={'budget': 200}))
        before = manager.store.revision(task_id)['snapshot']
        sent = len(places.sent)
        # Recompute uses the application's graph holder; keep its serving lifespan
        # open rather than trying to load a graph after shutdown has stopped it.
        assert asyncio.run(manager.recompute(task_id, reason='fixture@1')) == [6, 7]
    after = manager.store.revision(task_id)['snapshot']
    assert len(places.sent) == sent
    assert after['trace']['recomputed']['boundary'] == 'replayed_from_ledger'
    assert after['trace']['isochroneHash'] == before['trace']['isochroneHash']
    assert after['isochrone']['geometry'] == before['isochrone']['geometry']
    # The samples stay counted as the original run's attempts.
    assert after['isochrone']['networkRequests'] == before['isochrone']['networkRequests']
    assert after['isochrone']['statistics']['replayedFromLedger'] is True
    assert 'rebuilt_from_stored_samples' in after['isochrone']['warnings']


def test_a_recompute_refuses_a_task_that_never_reported(tmp_path):
    from test_checkup_facilities import body, make_app, run
    app = make_app(tmp_path)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
    with pytest.raises(CheckupError) as refused:
        asyncio.run(app.state.checkups.recompute(task_id, reason='fixture@1'))
    assert refused.value.code == 'checkup_recompute_needs_report'
