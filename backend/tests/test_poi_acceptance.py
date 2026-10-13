"""Synthetic B1 acceptance regressions; never use a real transport or credential."""
import asyncio
from pathlib import Path

import pytest

from app.poi.artifacts import export_artifacts
from app.poi.models import PoiCollectRequest, RuntimeConfig
from app.poi.normalize import classify, merge_entities, normalize
from app.poi.planner import build_plan, parameters
from app.poi.provider import ReplayProvider, whitelist
from app.poi.runtime import PoiRuntime
from app.poi.service import collect_pois


def setup_run(**kwargs):
    request = PoiCollectRequest(coordinateSystem='bd09ll', **kwargs)
    config = RuntimeConfig(runId='acceptance')
    plan = build_plan(request, config)
    return request, config, plan, PoiRuntime(config, plan)


@pytest.mark.parametrize('name,tags', [
    ('合成小学餐馆', ['餐饮;小学主题餐厅']),
    ('合成商店', ['购物;小学用品']),
    ('合成商店', ['购物;药店设备']),
    ('合成药店', ['医疗;药店', '教育;中学']),
    ('合成小学正门', ['教育;小学']),
])
def test_misleading_tags_and_accessories_never_accepted(name, tags):
    assert classify(name, tags)[1] != 'accepted'


@pytest.mark.parametrize('name,tags,status', [
    ('合成小学', ['教育培训;小学'], 'accepted'),
    ('合成幼儿园', ['教育培训;幼儿园'], 'accepted'),
    ('合成中学', ['教育培训;中学;初中'], 'accepted'),
    ('合成大学', ['教育培训;大学'], 'accepted'),
    ('合成小学辅导班', ['教育培训;小学'], 'excluded'),
    ('合成农贸市场-1号门', ['购物;农贸市场'], 'excluded'),
    ('合成小学', ['教育培训;培训机构'], 'excluded'),
    ('合成培训机构', ['教育培训;培训机构'], 'excluded'),
])
def test_v3_parent_education_tag_is_not_a_training_business(name, tags, status):
    """The provider's container tag says where a row sits, not what it is.

    Every education POI a v3 query returns is tagged ``教育培训;<type>``. That
    container word is also this dictionary's name for the training category, so
    reading the raw tag parts as category evidence classified a whole real
    retrieval — 11 primary schools, 11 middle schools and 15 kindergartens at one
    coordinate — as training businesses and excluded every one of them.
    """
    assert classify(name, tags)[1] == status


def test_a_container_tag_alone_establishes_no_category():
    """The group word is not evidence about a business, in either direction."""
    # Nothing but the container tag: no category is established, so no verdict is
    # invented — the record stays for review instead of being thrown away.
    assert classify('合成机构', ['教育培训'])[1] == 'needs_review'
    # …and a name that really does say training is still excluded by it.
    assert classify('合成教育科技', ['教育培训'])[1] == 'excluded'


@pytest.mark.parametrize('name,category,status', [
    ('合成大学', 'college', 'accepted'),
    ('合成职业技术学院', 'college', 'accepted'),
    ('合成培训学院', None, 'excluded'),
    ('合成大学继续教育学院', None, 'needs_review'),
])
def test_the_college_tag_carries_the_category_but_not_a_business(name, category, status):
    """``高等院校`` is the provider's word for what this dictionary calls ``高等教育``.

    It is the tag a real v3 education query returns for a university: every college
    at one surveyed coordinate came back as ``教育培训;高等院校``. Without the alias
    the tag established no category, so eight colleges were reported
    ``needs_review``/``insufficient_category_evidence`` instead of being counted —
    the container tag alone establishes nothing, exactly as above.
    """
    verdict = classify(name, ['教育培训;高等院校'])
    assert (verdict[0], verdict[1]) == (category, status)
    if status == 'accepted':
        # The category must be established by the tag as well as by the name, or an
        # education row would still be accepted only when its name happens to say so.
        assert 'tag:college' in verdict[2]


def test_uid_name_address_and_parent_conflicts_are_preserved():
    request, _, plan, _ = setup_run()
    base = {'uid': 'synthetic-conflict', 'name': '合成药店甲', 'address': '合成地址甲',
            'location': {'lng': 121.514, 'lat': 31.313},
            'detail_info': {'classified_poi_tag': '医疗;药店', 'parent_id': 'synthetic-parent-a'}}
    other = {**base, 'name': '合成药店乙', 'address': '合成地址乙',
             'detail_info': {**base['detail_info'], 'parent_id': 'synthetic-parent-b'}}
    records = [normalize(row, {'tileId': 'r0c0', 'query': '药店', 'pageNum': i}, 'synthetic')
               for i, row in enumerate([base, other])]
    accepted, review, _, _, _ = merge_entities(records, request, plan)
    assert not accepted and len(review) == 1
    assert {'uid_name_conflict', 'uid_address_conflict', 'uid_parent_conflict'} <= set(review[0]['conflicts'])


def test_raw_and_navigation_points_are_candidates_and_never_an_entrance():
    row = {'uid': 'synthetic-entrance', 'name': '合成药店', 'address': '合成地址甲',
           'location': {'lng': 121.514, 'lat': 31.313},
           'detail_info': {'classified_poi_tag': '医疗;药店',
                           'navi_location': {'lng': 121.5142, 'lat': 31.3132}}}
    record = normalize(row, {'tileId': 'r0c0', 'query': '药店', 'pageNum': 0}, 'synthetic')
    assert record['location'] == {'lng': 121.514, 'lat': 31.313}
    # §4.3: the provider's navigation point is guidance and may be a pickup
    # point, so it is a candidate. No place query establishes a walking entrance,
    # and null says exactly that — an empty list would claim there is none.
    assert record['navigationLocation'] == {'lng': 121.5142, 'lat': 31.3132}
    assert record['confirmedEntrances'] is None
    assert record['operatingStatus'] == 'unknown'


def test_an_absent_or_invalid_navigation_point_stays_unknown():
    provenance = {'tileId': 'r0c0', 'query': '药店', 'pageNum': 0}
    row = {'uid': 'synthetic-no-entrance', 'name': '合成药店', 'address': '合成地址甲',
           'location': {'lng': 121.514, 'lat': 31.313},
           'detail_info': {'classified_poi_tag': '医疗;药店'}}
    absent = normalize(row, provenance, 'synthetic')
    assert absent['navigationLocation'] is None and absent['confirmedEntrances'] is None
    assert absent['warnings'] == []
    broken = {**row, 'detail_info': {**row['detail_info'],
                                    'navi_location': {'lng': 200, 'lat': 31.313}}}
    invalid = normalize(broken, provenance, 'synthetic')
    # A guidance point outside the coordinate range is reported, not repaired and
    # not silently turned into a confirmed candidate.
    assert invalid['navigationLocation'] is None
    assert invalid['warnings'] == ['invalid_navigation_location']


def test_a_merged_entity_keeps_each_observations_candidates_separately():
    request, _, plan, _ = setup_run()
    base = {'uid': 'synthetic-candidates', 'name': '合成药店', 'address': '合成地址甲',
            'location': {'lng': 121.514, 'lat': 31.313},
            'detail_info': {'classified_poi_tag': '医疗;药店'}}
    guilded = {**base, 'detail_info': {**base['detail_info'],
                                      'navi_location': {'lng': 121.5142, 'lat': 31.3132}}}
    records = [normalize(row, {'tileId': 'r0c0', 'query': '药店', 'pageNum': i}, 'synthetic')
               for i, row in enumerate([base, guilded])]
    accepted, _, _, _, _ = merge_entities(records, request, plan)
    assert len(accepted) == 1
    candidates = accepted[0]['observations']
    assert len(candidates) == 2
    assert sorted(c['navigationLocation'] is None for c in candidates) == [False, True]
    assert all(c['confirmedEntrances'] is None for c in candidates)


def test_subset_request_keeps_other_categories_out_of_production_list():
    request, _, plan, runtime = setup_run(categories=['primary_school'])
    row = {'uid': 'synthetic-pharmacy', 'name': '合成药店',
           'location': {'lng': 121.514, 'lat': 31.313},
           'detail_info': {'classified_poi_tag': '医疗;药店'}}
    fixture = {'source': 'synthetic', 'pages': {'小学:0':
               {'status': 0, 'result_type': 'poi_type', 'total': 1, 'results': [row]}}}
    result = asyncio.run(collect_pois(request, ReplayProvider(fixture), runtime))
    assert not result.pois
    assert result.excluded_candidates[0]['classificationEvidence'] == ['category_not_requested']


def test_wire_coordinates_use_six_decimals_and_preserve_plan_precision():
    _, _, plan, _ = setup_run()
    sequence = plan['sequences'][0]
    lat, lng = parameters(sequence, 0)['location'].split(',')
    assert lat == f"{sequence['center'][1]:.6f}"
    assert lng == f"{sequence['center'][0]:.6f}"


def test_failed_page_is_locatable_separately_from_successful_pages():
    request, _, _, runtime = setup_run(categories=['primary_school'])
    fixture = {'source': 'synthetic', 'pages': {'小学:0':
               {'status': 0, 'result_type': 'poi_type', 'total': 1, 'results': []}}}
    result = asyncio.run(collect_pois(request, ReplayProvider(fixture), runtime))
    meta = result.query_coverage[0]
    assert meta['requestedPages'] == [0] and meta['successfulPages'] == [0]
    assert meta['reportedTotals'] == [{'pageNum': 0, 'total': 1}]
    assert meta['status'] == 'partial'
    failed = asyncio.run(collect_pois(request, ReplayProvider({'source': 'synthetic', 'pages': {}}),
                                    setup_run(categories=['primary_school'])[3]))
    assert failed.query_coverage[0]['pageErrors'] == [{'pageNum': 0, 'reason': 'fixture_missing'}]


def test_nested_transport_fields_do_not_escape_whitelist():
    payload = {'status': 0, 'results': [{'uid': 'synthetic-1', 'name': '合成药店',
        'location': {'lng': 121.514, 'lat': 31.313, 'unrelated': 'private'},
        'address': {'unrelated': 'private'},
        'detail_info': {'parent_id': {'unrelated': 'private'},
                        'classified_poi_tag': ['private'],
                        'navi_location': {'lng': 121.514, 'lat': 31.313, 'unrelated': 'private'}}}]}
    result = whitelist(payload)
    assert 'private' not in str(result)
    assert result['results'][0]['location'] == {'lng': 121.514, 'lat': 31.313}


def test_export_failure_does_not_publish_incomplete_result(tmp_path, monkeypatch):
    request, config, plan, runtime = setup_run()
    result = asyncio.run(collect_pois(request, ReplayProvider.from_path('tests/fixtures/poi/collection.json'), runtime))
    original = Path.write_text
    def fail_csv(path, *args, **kwargs):
        if path.name == 'poi-list.csv':
            raise OSError('synthetic disk failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', fail_csv)
    output = tmp_path / 'published'
    with pytest.raises(OSError):
        export_artifacts(output, config, plan, result, runtime)
    assert not output.exists()
