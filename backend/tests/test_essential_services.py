"""Named services cannot inherit a broad category's coverage or verification."""
import pytest
from app import catalog
from app.checkups import accessibility_stage as stage
from app.checkups.essential import matches
from app.checkups.retention import summary_of
from test_accessibility_stage import (boundary, dry_layer, facility, road_store)


def context(**updates):
    value = {'queryStatus': 'completed', 'queryIncompleteRegions': {},
        'queryDomain': {'paddingMeters': 1300}, 'reviewCandidates': [], 'quarantine': [],
        'queryCoverage': [{'category': key, 'query': catalog.BY_KEY[key].query}
                          for key in ('market', 'pharmacy', 'school', 'combined_school')]}
    return {**value, **updates}


def assess(items, evidence=None):
    outcome = stage.assess_accessibility(geometry=boundary(0, -50, 1500, 50),
        facilities=items, query_status='complete', majors=('shopping', 'medical', 'education'),
        store=road_store(), version='essential-test', obstacles=dry_layer(),
        essential_context=context() if evidence is None else evidence)
    return outcome.essential_services


def row(result, key):
    return next(r for r in result.accessibility.categories if r.category == key)


def test_other_facilities_do_not_substitute_and_empty_search_is_a_suspected_gap():
    value = assess([facility(100, category=key, facility_id=key)
                    for key in ('supermarket', 'clinic', 'preschool')])
    for key in ('market', 'pharmacy', 'primary_school'):
        r = row(value, key)
        assert r.covered_m2 == 0 and r.gap_m2 > 0
        assert r.covered_m2 + r.gap_m2 + r.unknown_m2 == pytest.approx(value.accessibility.domain_area_m2)
    assert value.verification_status == 'model_only'
    assert all(zone.suspected for zone in value.service_gaps.zones)


def test_named_sources_and_nearby_sources_are_usable():
    result = assess([facility(100, category=key, facility_id='合成九年一贯制学校' if key == 'combined_school' else key)
                     for key in ('market', 'pharmacy', 'combined_school')])
    assert all(r.covered_m2 > 0 for r in result.accessibility.categories)
    primary = catalog.ESSENTIAL_SERVICES[2]
    assert matches({'category': 'primary_school'}, primary)
    assert matches({'category': 'combined_school', 'name': '合成九年一贯制学校'}, primary)
    assert not matches({'category': 'combined_school', 'name': '合成初中部'}, primary)
    assert not matches({'category': 'preschool'}, primary)
    assert not matches({'category': 'hospital_pharmacy'}, catalog.ESSENTIAL_SERVICES[1])
    assert not matches({'category': 'pharmacy', 'name': '合成医院西院药房'}, catalog.ESSENTIAL_SERVICES[1])


@pytest.mark.parametrize('evidence', [context(queryCoverage=[]),
    context(queryStatus='partial', queryIncompleteRegions=None),
    context(reviewCandidates=[{'category': None, 'provenance': [{'category': 'market'}]}]),
    context(queryDomain={'paddingMeters': 0})])
def test_missing_queries_or_ambiguous_candidate_do_not_prove_absence(evidence):
    r = row(assess([], evidence), 'market')
    assert r.gap_m2 == 0 and r.unknown_m2 > 0


def test_unresolved_entrance_is_unknown_even_after_completed_search():
    r = row(assess([facility(100, dy=200, category='market')]), 'market')
    assert r.covered_m2 == r.gap_m2 == 0 and r.unknown_m2 > 0


@pytest.mark.parametrize('category,key,name', [('combined_school','primary_school','合成初中部'),
    ('pharmacy','pharmacy','合成医院西院药房')])
def test_unconfirmed_target_capability_does_not_become_empty_directory(category,key,name):
    candidate = facility(750, category=category)
    candidate['name'] = name
    r = row(assess([candidate]), key)
    assert r.covered_m2 == r.gap_m2 == 0 and r.unknown_m2 > 0


def test_retained_special_summary_has_no_coordinates_or_geometries():
    value = assess([]).model_dump(mode='json', by_alias=True)
    retained = summary_of({'essentialServices': value})['essentialServices']
    assert retained['accessibility']['categories']
    assert 'coordinates' not in str(retained) and 'geometry' not in str(retained)


def test_incomplete_target_page_blocks_only_its_reachable_service_range():
    evidence = context(queryStatus='partial', queryIncompleteRegions={
        'market': [boundary(-500, -100, -400, 100)]})
    result = assess([], evidence)
    market = row(result, 'market')
    assert market.unknown_m2 > 0 and market.gap_m2 > 0
    assert row(result, 'pharmacy').gap_m2 > market.gap_m2
    assert market.covered_m2 + market.gap_m2 + market.unknown_m2 == pytest.approx(
        result.accessibility.domain_area_m2)


def test_named_services_keep_the_distance_uncertainty_band():
    outcome = stage.assess_accessibility(geometry=boundary(0, -50, 1500, 50),
        facilities=[facility(0, category='market')], query_status='complete',
        majors=('shopping','medical','education'), store=road_store(), version='essential-test',
        obstacles=dry_layer(), essential_context=context()).essential_services
    market = row(outcome, 'market')
    assert market.covered_m2 > 0 and market.gap_m2 > 0 and market.unknown_m2 > 0


def test_ambiguous_nearby_candidate_is_preserved_before_special_assessment():
    from types import SimpleNamespace
    from app.poi.normalize import merge_entities, normalize
    record = normalize({'uid':'review-nearby','name':'合成药店甲',
        'location':{'lng':121.514,'lat':31.313},
        'detail_info':{'classified_poi_tag':'医疗;药店'}},
        {'tileId':'r0c0','category':'pharmacy','query':'药店','pageNum':0}, 'synthetic')
    other = {**record, 'name':'合成药店乙'}
    request = SimpleNamespace(center=SimpleNamespace(lng=121.514,lat=31.313), categories=['pharmacy'])
    nearby = []
    accepted, review, _, outside, _ = merge_entities([record, other], request,
        within=lambda _:False, nearby=nearby)
    assert not accepted and not nearby and len(outside) == len(review) == 1
    assert review[0]['countingRegion'] == 'outside' and review[0]['provenance'][0]['category'] == 'pharmacy'
