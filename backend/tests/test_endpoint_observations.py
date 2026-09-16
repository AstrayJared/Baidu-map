import copy
import random

import pytest
from life_circle.coordinates import LocalProjection
from tools.endpoint_observations import build_layer, diagnose_triangle, ExperimentCancelled

ORIGIN=(121.513926,31.313077)
PROJECTION=LocalProjection(ORIGIN)


def event(i=1, xy=(100,100), actual=None, duration=800):
    return dict(id=i, origin=list(ORIGIN), destination=list(PROJECTION.to_geographic(xy)),
        route_origin=list(ORIGIN), route_destination=list(PROJECTION.to_geographic(actual or xy)),
        duration=duration, outcome='success', endpoint_verified=True, reservedAt='2026-09-13T00:00:00+00:00')


def test_zero_and_small_offset_preserve_requested_coordinates():
    events=[event(),event(2,(200,100),(212,100))]
    before=copy.deepcopy(events)
    layer=build_layer(events,'fixture',ORIGIN,1600)
    assert events==before
    assert layer['records'][0]['request_pair_exactly_supported']
    assert not layer['records'][1]['request_pair_exactly_supported']
    assert layer['records'][1]['request_destination']==events[1]['destination']
    assert layer['records'][1]['actual_destination']==events[1]['route_destination']


def test_duplicate_support_keeps_all_observations():
    layer=build_layer([event(),event(2,(110,100),(100,100))],'fixture',ORIGIN,1600)
    assert layer['summary']['usable_records']==2
    assert layer['summary']['spatial_points']==1
    assert layer['points'][0]['record_ids']==['fixture:1','fixture:2']


@pytest.mark.parametrize('duration',[801,1100])
def test_conflict_never_chooses_minimum(duration):
    layer=build_layer([event(),event(2,duration=duration)],'fixture',ORIGIN,1600)
    point=layer['points'][0]
    assert point['conflict'] and point['duration'] is None
    assert point['crosses_threshold']==(duration>900)


def test_drifting_origins_stay_separate():
    a,b=event(),event(2)
    b['route_origin']=list(PROJECTION.to_geographic((1,0)))
    layer=build_layer([a,b],'fixture',ORIGIN,1600)
    assert layer['summary']['origin_groups']==2 and len(layer['points'])==2


@pytest.mark.parametrize('change,issue',[
    ({'route_destination':None},'missing_actual_destination'),
    ({'route_origin':None},'missing_actual_origin'),
    ({'duration':None},'invalid_duration'),
    ({'duration':True},'invalid_duration'),
    ({'endpoint_verified':False},'endpoints_unverified'),
    ({'route_destination':[181,31]},'invalid_actual_destination'),
    ({'route_destination':list(PROJECTION.to_geographic((1601,100)))},'outside_extent'),
    ({'route_destination':list(PROJECTION.to_geographic((151,100)))},'endpoint_offset'),
])
def test_invalid_evidence_is_retained_but_not_used(change,issue):
    item=event();item.update(change)
    layer=build_layer([item],'fixture',ORIGIN,1600)
    assert issue in layer['records'][0]['issues']
    assert not layer['points']


def test_refused_duration_is_not_invented_from_diagnostic_endpoint_pair():
    item=event();item.update(outcome='endpoint_offset',duration=None,route_destination=None,
        route_endpoints=[{'start':list(ORIGIN),'end':list(PROJECTION.to_geographic((180,100)))}])
    record=build_layer([item],'fixture',ORIGIN,1600)['records'][0]
    assert record['duration'] is None and record['actual_destination'] is None
    assert len(record['candidate_endpoints'])==1


def test_order_independence_and_duplicate_identity_rejection():
    events=[event(i,(100+i,100)) for i in range(10)]
    expected=build_layer(events,'fixture',ORIGIN,1600)
    random.Random(20260911).shuffle(events)
    assert build_layer(events,'fixture',ORIGIN,1600)==expected
    with pytest.raises(ValueError): build_layer([event(),event()],'fixture',ORIGIN,1600)


def test_cancellation_discards_partial_layer():
    calls=[0]
    def cancelled():
        calls[0]+=1
        return calls[0]>2
    with pytest.raises(ExperimentCancelled):
        build_layer([event(i) for i in range(5)],'fixture',ORIGIN,1600,cancelled=cancelled)


@pytest.mark.parametrize('actual,reason',[
    ([(0,0),(10,0),(0,10)],'regular'),
    ([(0,0),(0,0),(0,10)],'degenerate'),
    ([(0,0),(10,0),(20,0)],'degenerate'),
    ([(0,0),(0,10),(10,0)],'flipped'),
])
def test_triangle_diagnostics_do_not_generate_support(actual,reason):
    requested=[(0,0),(10,0),(0,10)]
    records=build_layer([event(i,p,q) for i,(p,q) in enumerate(zip(requested,actual))],
        'fixture',ORIGIN,1600)['records']
    assert diagnose_triangle(records,ORIGIN)['status']==reason


def test_multiple_origins_prevent_triangle_support():
    items=[event(i,p) for i,p in enumerate([(0,0),(10,0),(0,10)])]
    items[1]['route_origin']=list(PROJECTION.to_geographic((1,0)))
    assert diagnose_triangle(build_layer(items,'fixture',ORIGIN,1600)['records'],ORIGIN)['status']=='mixed_origins'


def test_runner_freezes_inputs_blocks_reuse_and_preserves_data(tmp_path):
    import json
    from tools.endpoint_observation_experiment import execute, digest
    source=tmp_path/'input.json'
    source.write_text(json.dumps({'events':[dict(event(),phase='adaptive'),dict(event(2),phase='reference')]}))
    output=tmp_path/'run'
    result=execute(output,{'fixture':(source,digest(source))})
    assert result['passed'] and result['excluded_reference_events']==1
    assert result['sources_unchanged']
    assert len(result['synthetic_cases'])==8
    assert result['cohorts']['fixture-adaptive']['records']==1
    with pytest.raises(FileExistsError): execute(output,{'fixture':(source,digest(source))})


def test_runner_hash_failure_does_not_produce_a_result(tmp_path):
    from tools.endpoint_observation_experiment import execute
    source=tmp_path/'input.json';source.write_text('{}')
    with pytest.raises(ValueError): execute(tmp_path/'run',{'fixture':(source,'bad-hash')})
    assert not (tmp_path/'run'/'summary.json').exists()


def test_network_is_blocked_in_experiment():
    import socket
    from tools.diagnostic_common import no_network, DiagnosticStop
    with no_network(), pytest.raises(DiagnosticStop):
        socket.create_connection(('127.0.0.1',9))


def test_raw_error_and_invalid_timestamp_are_not_exported():
    item=event(); item.update(outcome='fixture-secret',reservedAt='fixture-secret',message='fixture-secret')
    layer=build_layer([item],'fixture',ORIGIN,1600)
    assert 'fixture-secret' not in str(layer)
