import asyncio
import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError
from life_circle.coordinates import LocalProjection

from app.poi.models import CATEGORIES, PoiCollectRequest, RuntimeConfig
from app.poi.normalize import coordinate, classify, inside, merge_entities, normalize
from app.poi.planner import build_plan, parameters
from app.poi.provider import ReplayProvider
from app.poi.runtime import PoiRuntime
from app.poi.service import collect_pois
from app.request_control import RequestStopped

FIXTURE = Path(__file__).parent / 'fixtures/poi/collection.json'


def context(**overrides):
    request = PoiCollectRequest(coordinateSystem='bd09ll', categories=['market', 'pharmacy', 'primary_school'])
    config = RuntimeConfig(runId='test-run', **overrides)
    return request, config, build_plan(request, config)


def collect(**overrides):
    request, config, plan = context(**overrides)
    runtime = PoiRuntime(config, plan)
    return asyncio.run(collect_pois(request, ReplayProvider.from_path(FIXTURE), runtime)), runtime


def test_complete_offline_pipeline_has_explicit_quality_and_uid_uniqueness():
    result, runtime = collect()
    assert result.query_status == 'completed'
    assert result.catalog_completeness == 'unverified'
    assert {c: n for c, n in result.counts_by_category.items() if n} == {
        'market': 1, 'pharmacy': 2, 'primary_school': 1}
    assert set(result.counts_by_category) == set(CATEGORIES)
    assert len({p['id'] for p in result.pois}) == 4
    assert len(result.review_candidates) == 1 and len(result.excluded_candidates) == 1
    assert result.statistics['invalidRecords'] == 32
    assert result.statistics['possibleDuplicateGroups'] == 1
    assert result.statistics['attempts'] == 144
    assert result.statistics['networkReservations'] == result.statistics['confirmedSent'] == 0
    assert result.statistics['responseMedianMs'] is None
    assert all(e['pageNum'] == 0 for e in runtime.events[:112])
    assert all(e['pageNum'] == 1 for e in runtime.events[112:])
    pharmacy = next(p for p in result.pois if p['category'] == 'pharmacy')
    assert len(pharmacy['provenance']) == 32
    assert pharmacy['operatingStatus'] == 'unknown' and pharmacy['navigationLocation'] is None
    assert not any('reachable' in k.lower() for k in result.model_dump())


def test_fixture_replay_is_deterministic():
    first, _ = collect()
    second, _ = collect()
    for result in (first, second):
        result.started_at = result.finished_at = 'ignored'
        result.statistics['limiterWaitMs'] = 0
    assert first.model_dump() == second.model_dump()


def test_total_budget_and_category_budget_preserve_other_categories():
    result, _ = collect(totalBudget=1)
    assert result.query_status == 'partial' and result.stop_reason == 'total_budget'
    assert result.statistics['attempts'] == 1
    # 预算必须逐类给全（RuntimeConfig.budgets 的契约），所以从词典派生默认，
    # 只把 market 压低：断言逐条看的是"某类被压到 1 之后别的类不受影响"。
    budgets = dict.fromkeys(CATEGORIES, 100)
    budgets['market'] = 1
    result, _ = collect(categoryBudgets=budgets)
    assert result.query_status == 'partial'
    assert result.counts_by_category['primary_school'] == 1
    assert result.statistics['countsByCategory']['market'] == 1
    assert any(q['stopReason'] == 'category_budget' for q in result.query_coverage)


def test_empty_success_and_missing_fixture_are_different():
    request, config, plan = context()
    empty = {'status': 0, 'results': [], 'total': 0}
    pages = {s['query']+':0': empty for s in plan['sequences']}
    result = asyncio.run(collect_pois(request, ReplayProvider({'source':'synthetic','pages':pages}), PoiRuntime(config,plan)))
    assert result.query_status == 'completed' and result.pois == []
    result = asyncio.run(collect_pois(request, ReplayProvider({'source':'synthetic','pages':{}}), PoiRuntime(config,plan)))
    assert result.query_status == 'failed' and result.pois == []


def test_plan_grid_covers_all_corners_and_clips_inclusively():
    request, config, plan = context()
    projection = LocalProjection((request.center.lng, request.center.lat))
    assert len(plan['sequences']) == 112 and plan['requestUpperBound'] == 300
    assert plan['searchExtent']['localMeters'] == [-2600,-2600,2600,2600]
    for sequence in plan['sequences']:
        x0,y0,x1,y1 = sequence['localMeters']
        center = projection.to_local(sequence['center'])
        assert sequence['radius'] == 925
        for corner in ((x0,y0),(x0,y1),(x1,y0),(x1,y1)):
            assert math.dist(center,corner) < sequence['radius']
    for x,y in ((2600,2600),(-2600,-2600),(2600,-2600),(-2600,2600)):
        lng,lat = projection.to_geographic((x,y))
        assert inside({'lng':lng,'lat':lat},plan['searchExtent']['localMeters'],projection)
    lng,lat = projection.to_geographic((2600.001,0))
    assert not inside({'lng':lng,'lat':lat},plan['searchExtent']['localMeters'],projection)
    assert parameters(plan['sequences'][0],0)['location'].startswith(str(round(plan['sequences'][0]['center'][1],5))[:6])
    smoke = build_plan(request, config.model_copy(update={'phase':'smoke'}))
    assert len(smoke['sequences']) == 3


@pytest.mark.parametrize('data', [{}, {'coordinateSystem':'wgs84'}, {'coordinateSystem':'bd09ll','url':'https://invalid'},
    {'coordinateSystem':'bd09ll','categories':['pharmacy','pharmacy']},
    {'coordinateSystem':'bd09ll','center':{'lng':True,'lat':31}},
    {'coordinateSystem':'bd09ll','analysisHalfWidthMeters':2000}])
def test_invalid_requests_are_rejected(data):
    with pytest.raises(ValidationError):
        PoiCollectRequest.model_validate(data)


@pytest.mark.parametrize('value',[True,False,'', '1e2','NaN','Infinity',float('nan'),float('inf'),[],{},None,181])
def test_coordinate_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        coordinate(value,-180,180)


@pytest.mark.parametrize('value',[121,121.5,'121.5',' +121.50 '])
def test_coordinate_accepts_finite_decimal_values(value):
    assert coordinate(value,-180,180) == float(value)


@pytest.mark.parametrize('name,tags,status',[
    ('合成药店',['医疗;药店'],'accepted'),('合成药店',[],'needs_review'),
    ('合成医院门诊药房',['药店'],'needs_review'),('九年一贯制学校',['小学'],'accepted'),
    ('合成生鲜超市',['菜市场'],'needs_review'),('合成小学北门',['小学'],'excluded'),
    ('合成小学辅导班',['小学'],'excluded'),('合成药店',['小学'],'needs_review')])
def test_classification_evidence(name,tags,status):
    assert classify(name,tags)[1] == status


def test_uid_conflicts_and_different_campuses_remain_explicit():
    request, _, plan = context()
    provenance = {'tileId':'r0c0','query':'小学','pageNum':0}
    def item(uid, name, lng, tag='小学'):
        return normalize({'uid':uid,'name':name,'location':{'lng':lng,'lat':31.313},
                          'detail_info':{'classified_poi_tag':tag}},provenance,'synthetic')
    records = [item('a','合成小学',121.514),item('a','合成小学',121.515),
               item('b','合成小学东校区',121.514),item('c','合成小学西校区',121.516),
               item('d','合成药店',121.514,'药店'),item('d','合成小学',121.514)]
    accepted,review,_,_,_ = merge_entities(records,request,plan)
    assert {p['sourceUid'] for p in accepted} == {'b','c'}
    assert {p['sourceUid'] for p in review} == {'a','d'}
    assert all(len(p['observations']) == 2 for p in review)


def test_plan_mutation_and_duplicate_runtime_are_rejected():
    request,config,plan=context()
    broken=json.loads(json.dumps(plan));broken['sequences'][0]['radius']=999
    with pytest.raises(RequestStopped, match='plan_mismatch'):
        PoiRuntime(config,broken)
    runtime=PoiRuntime(config,plan)
    asyncio.run(collect_pois(request,ReplayProvider.from_path(FIXTURE),runtime))
    with pytest.raises(RequestStopped,match='run_already_started'):
        asyncio.run(collect_pois(request,ReplayProvider.from_path(FIXTURE),runtime))
