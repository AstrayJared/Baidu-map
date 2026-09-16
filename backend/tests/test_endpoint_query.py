import pytest
import json

from tools.endpoint_query import ObservationIndex, QueryModel
from tools.endpoint_preserved_triangulation import preserved_network
from shapely.geometry import box


def square(values=(700,800,1000,1100)):
    data=[dict(id=str(i),xy=p,duration=t,origin='o') for i,(p,t) in enumerate(zip([(0,0),(0,2),(2,0),(2,2)],values))]
    return data,preserved_network(data,box(-1,-1,3,3))


def rows():
    return [dict(id='a',xy=[10,20],duration=700,origin='origin-1'),
            dict(id='alias',xy=[10,20],duration=700,origin='origin-1')]


def test_exact_observation_keeps_aliases_and_is_not_ground_truth():
    index=ObservationIndex(rows())
    r=index.lookup([10,20],origin='origin-1')
    assert r['evidence']=='observed' and r['source_ids']==['a','alias']
    assert r['duration_seconds']==700 and r['confidence'] is None
    assert r['observed_kind']=='api_route_observation'


def test_nearby_request_is_not_an_observed_destination():
    index=ObservationIndex(rows())
    assert index.lookup([10.00000000001,20],origin='origin-1') is None


def test_conflicting_point_is_not_silently_interpolated_or_averaged():
    data=rows()+[dict(id='conflict',xy=[10,20],duration=1100,origin='origin-1')]
    r=ObservationIndex(data).lookup([10,20],origin='origin-1')
    assert r['evidence']=='unknown' and r['reason']=='conflicting_observations'
    assert r['duration_seconds'] is None and r['source_ids']==['a','alias','conflict']


def test_incompatible_origin_is_an_error_not_unknown_or_a_prediction():
    with pytest.raises(ValueError,match='origin'):
        ObservationIndex(rows()).lookup([10,20],origin='different')


@pytest.mark.parametrize('xy',[[float('nan'),20],[10,float('inf')],[10],['10',20]])
def test_invalid_query_is_an_input_error(xy):
    with pytest.raises(ValueError):ObservationIndex(rows()).lookup(xy,origin='origin-1')


def test_record_mutation_cannot_change_frozen_observation():
    data=rows();index=ObservationIndex(data);data[0]['duration']=500
    assert index.lookup([10,20],origin='origin-1')['duration_seconds']==700


def test_interpolation_is_reproducible_and_not_an_observation():
    data,network=square();m=QueryModel(data,network,box(-1,-1,3,3),radius=None)
    r=m.query([.4,.6],origin='o')
    assert r['evidence']=='interpolated' and r['duration_seconds']==pytest.approx(790)
    assert sum(r['weights'])==pytest.approx(1)
    assert len(r['vertex_ids'])==len(r['source_ids'])==3 and r['confidence'] is None


def test_observation_survives_radius_filter_but_does_not_fill_neighborhood():
    data,network=square();m=QueryModel(data,network,box(-1,-1,3,3),radius=.1)
    assert m.query([0,0],origin='o')['evidence']=='observed'
    r=m.query([.001,.001],origin='o')
    assert r['evidence']=='unknown' and r['reason']=='radius_filtered'


def test_shared_edge_and_threshold_use_exact_classification():
    import math
    data,network=square((800,800,1000,1000));m=QueryModel(data,network,box(-1,-1,3,3),radius=None)
    assert m.query([1,1],origin='o')['within_threshold'] is True
    assert m.query([math.nextafter(1,2),1],origin='o')['within_threshold'] is False
    assert m.query([math.nextafter(1,0),1],origin='o')['within_threshold'] is True
    assert m.query([-1e-14,1],origin='o')['reason']=='outside_convex_hull'


def test_exact_conflict_blocks_otherwise_valid_interpolation():
    data,network=square();data.extend([dict(id='c1',xy=[1,1],duration=500,origin='o'),dict(id='c2',xy=[1,1],duration=1200,origin='o')])
    network=preserved_network(data,box(-1,-1,3,3));m=QueryModel(data,network,box(-1,-1,3,3),radius=None)
    assert m.query([1,1],origin='o')['reason']=='conflicting_observations'


def test_corrupt_model_is_not_unknown_even_at_observed_point():
    data,network=square();network['nodes']['p00000']['duration']=123
    with pytest.raises(ValueError,match='source'):QueryModel(data,network,box(-1,-1,3,3),radius=None)


def test_source_and_return_mutation_cannot_change_model():
    data,network=square();m=QueryModel(data,network,box(-1,-1,3,3),radius=None)
    data[0]['duration']=999;network['nodes']['p00000']['duration']=999
    r=m.query([0,0],origin='o');r['source_ids'].clear()
    assert m.query([0,0],origin='o')['duration_seconds']==700
    assert m.query([0,0],origin='o')['source_ids']==['0']


def test_batch_cancel_never_returns_a_partial_completed_batch():
    from tools.endpoint_observations import ExperimentCancelled
    data,network=square();m=QueryModel(data,network,box(-1,-1,3,3),radius=None)
    count=0
    def cancelled():
        nonlocal count
        count+=1
        return count>5
    with pytest.raises(ExperimentCancelled):m.query_many([[.1,.1],[.2,.2],[.3,.3]],origin='o',cancelled=cancelled)


def test_domain_coordinate_system_and_order_contract():
    data,network=square();m=QueryModel(data,network,box(-1,-1,3,3),radius=None)
    assert m.query([5,5],origin='o')['reason']=='outside_domain'
    with pytest.raises(ValueError):m.query([0,0],origin='o',coordinate_system='bd09ll')
    points=[[0,0],[1,1],[.5,.5]]
    assert m.query_many(points,origin='o')==list(reversed(m.query_many(list(reversed(points)),origin='o')))


def test_frozen_radius_policy_is_not_recomputed_across_a_rounding_boundary():
    import math
    data,network=square();r=math.sqrt(2)
    network['face_circumradii_m']=[math.nextafter(r,math.inf)]*2
    m=QueryModel(data,network,box(-1,-1,3,3),radius=r)
    assert m.query([.4,.6],origin='o')['reason']=='radius_filtered'


def test_thin_exact_triangle_and_near_edge_are_not_snapped():
    from tools.endpoint_preserved_triangulation import observations
    data=[dict(id=str(i),xy=p,duration=800+100*p[0],origin='o') for i,p in enumerate([(0,0),(1,0),(0,1e-13)])]
    nodes,_=observations(data,lambda:False)
    fixture=dict(status='candidate',nodes=nodes,all_triangles=[('p00000','p00002','p00001')])
    m=QueryModel(data,fixture,box(-1,-1,2,2),radius=None)
    assert m.query([.25,2.5e-14],origin='o')['duration_seconds']==825
    assert m.query([.25,-1e-30],origin='o')['reason']=='outside_convex_hull'


def test_collinear_observations_are_point_evidence_only():
    data=[dict(id=str(i),xy=[i,0],duration=800,origin='o') for i in range(3)]
    d=box(-1,-1,3,3);network=preserved_network(data,d);m=QueryModel(data,network,d,radius=None)
    assert m.query([1,0],origin='o')['evidence']=='observed'
    assert m.query([1.1,0],origin='o')['reason']=='insufficient_geometry'


def test_snapshot_hash_blocks_changed_model(tmp_path):
    from tools.endpoint_query import load_snapshot
    from tools.endpoint_fresh_mesh import export_result
    data,network=square();p=tmp_path/'model.json';p.write_text(json.dumps(export_result(network)))
    with pytest.raises(ValueError,match='hash'):
        load_snapshot(p,data,box(-1,-1,3,3),radius=None,expected_sha256='0'*64)
