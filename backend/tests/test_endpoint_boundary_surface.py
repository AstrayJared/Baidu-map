import asyncio

import pytest
from shapely.geometry import box,Point

from tools.endpoint_boundary_surface import connect_regions,face_key,add_origin_condition,compute_boundary_surface
from test_endpoint_boundary import session,Provider,ORIGIN
from life_circle.models import IsochroneRequest,CancelToken


def triangle(values=(800,800,800)):
    nodes={str(i):dict(actual=xy,duration=t,record_ids=[str(i)]) for i,(xy,t) in enumerate(zip([(0,0),(100,0),(0,100)],values))}
    return dict(nodes=nodes,all_triangles=[('0','1','2')],conflicts=[])


def test_mixed_triangle_is_unknown_even_with_reachable_center():
    net=triangle((800,1000,800));result=connect_regions(net,box(0,0,100,100),{'0:1:2':dict(xy=[30,30],duration=800)})
    assert result['reachable'].is_empty and result['unknown'].area==10000


def test_homogeneous_vertices_require_internal_actual_center():
    net=triangle();domain=box(0,0,100,100)
    assert connect_regions(net,domain,{})['reachable'].is_empty
    assert connect_regions(net,domain,{'0:1:2':dict(xy=[80,80],duration=800)})['reachable'].is_empty
    yes=connect_regions(net,domain,{'0:1:2':dict(xy=[30,30],duration=800)})
    assert yes['reachable'].area==5000 and yes['unknown'].area==5000


def test_new_counterevidence_and_failures_block_previously_accepted_face():
    net=triangle();centers={'0:1:2':dict(xy=[30,30],duration=800)}
    opposite=connect_regions(net,box(0,0,100,100),centers,witnesses=[dict(xy=[20,20],duration=1900)])
    assert opposite['reachable'].is_empty
    failed=connect_regions(net,box(0,0,100,100),centers,blocked_points=[(20,20)])
    assert failed['reachable'].is_empty


def test_polygon_union_retains_hole_components_and_domain_clipping():
    nodes={};faces=[];centers={}
    for x in range(3):
        for y in range(3):
            if (x,y)==(1,1):continue
            for points in [[(x,y),(x+1,y),(x+1,y+1)],[(x,y),(x+1,y+1),(x,y+1)]]:
                ids=[]
                for xy in points:
                    i=str(len(nodes));nodes[i]=dict(actual=xy,duration=800);ids.append(i)
                faces.append(ids);centers[face_key(ids)]=dict(xy=[sum(p[0] for p in points)/3,sum(p[1] for p in points)/3],duration=800)
    ids=[]
    for xy in [(5,0),(6,0),(5,1)]:
        i=str(len(nodes));nodes[i]=dict(actual=xy,duration=800);ids.append(i)
    faces.append(ids);centers[face_key(ids)]=dict(xy=[5.2,.2],duration=800)
    result=connect_regions(dict(nodes=nodes,all_triangles=faces,conflicts=[]),box(0,0,5.5,3),centers)
    assert len(result['reachable'].geoms)==2
    assert sum(len(p.interiors) for p in result['reachable'].geoms)==1
    assert result['reachable'].intersection(result['unknown']).area==0
    assert result['reachable'].bounds[2]==5.5


def test_origin_zero_is_explicit_model_constraint_not_api_record():
    async def run():
        s,_,_=session();await s.measure((-100,-100),'initial');n=len(s.observations)
        record=add_origin_condition(s)
        assert record['duration']==0 and record['source_kind']=='model_origin_condition'
        assert len(s.observations)==n
        assert add_origin_condition(s)==record
    asyncio.run(run())


def test_complete_synthetic_surface_is_valid_and_contains_no_opposite_observed_point():
    async def run():
        provider=Provider(lambda x,y:0 if abs(x)+abs(y)<1e-6 else (400 if x<17 else 1900))
        request=IsochroneRequest(ORIGIN,'bd09ll',budget=200,extent=200,max_extent=200,coarse_size=100,min_size=50,concurrency=1)
        result=await compute_boundary_surface(request,provider,CancelToken())
        from shapely.geometry import shape
        reach=shape(result['geometry']);unknown=shape(result['unknownRegion'])
        assert reach.is_valid and reach.intersection(unknown).area<1e-18
        assert result['statistics']['requests']==provider.calls<=200
        assert result['boundaryModel']['algorithm']=='actual-endpoint-boundary-surface-e61'
        assert all(not reach.contains(Point(o.route_destination)) for o in result['_observations'] if o.observed_duration and o.observed_duration>900)
    asyncio.run(run())


def test_cancelled_surface_does_not_publish_geometry():
    async def run():
        token=CancelToken();token.cancel()
        r=await compute_boundary_surface(IsochroneRequest(ORIGIN,'bd09ll',budget=200),Provider(),token)
        assert r['status']=='cancelled' and r['geometry'] is None
    asyncio.run(run())


def test_live_budget_persists_and_second_run_is_forbidden(tmp_path):
    from tools.endpoint_boundary_surface_live import SurfaceLedger
    from tools.live_smoke import LiveGuardError
    from test_endpoint_live import request
    with SurfaceLedger(tmp_path) as ledger:
        ledger.arm()
        with pytest.raises(LiveGuardError):ledger.arm()
        ledger.data['counts']['analysis']=799
        ledger.reserve('analysis',request(ORIGIN),1)
        with pytest.raises(LiveGuardError):ledger.reserve('analysis',request(ORIGIN),2)
    with SurfaceLedger(tmp_path) as ledger:
        assert ledger.data['counts']['analysis']==800
        with pytest.raises(LiveGuardError):ledger.arm()


def test_live_guard_rejects_cancel_and_outside_domain_before_send(tmp_path):
    from tools.endpoint_boundary_surface_live import SurfaceLedger
    from tools.live_smoke import LiveGuardError
    from test_endpoint_live import request
    with SurfaceLedger(tmp_path) as ledger:
        ledger.arm()
        with pytest.raises(LiveGuardError):ledger.reserve('analysis',request((121.6,31.4)),1)
        ledger.token.cancel()
        with pytest.raises(LiveGuardError):ledger.reserve('analysis',request(ORIGIN),2)
        assert ledger.data['counts']['analysis']==0


def test_live_guard_stops_rate_limit_and_redacts_key(tmp_path):
    import httpx
    from tools.endpoint_boundary_surface_live import SurfaceLedger
    from tools.endpoint_live import EndpointTransport
    from tools.live_smoke import LiveGuardError
    from test_endpoint_live import request
    calls=[]
    async def run():
        with SurfaceLedger(tmp_path) as ledger:
            ledger.arm()
            transport=EndpointTransport(ledger,httpx.MockTransport(lambda r: (calls.append(1),httpx.Response(200,json={'status':401}))[1]))
            transport.phase='analysis'
            await transport.handle_async_request(request(ORIGIN))
            assert ledger.token.cancelled
            with pytest.raises(LiveGuardError):await transport.handle_async_request(request(ORIGIN))
            assert len(calls)==1
            assert 'fixture-not-a-key' not in ledger.file.read_text(encoding='utf-8')
    asyncio.run(run())


def test_final_result_serializes_without_private_observations():
    import json
    from tools.endpoint_boundary_surface_live import public_result
    async def run():
        result=await compute_boundary_surface(IsochroneRequest(ORIGIN,'bd09ll',budget=200,extent=200,max_extent=200,coarse_size=100,min_size=50),Provider(),CancelToken())
        wire=public_result(result)
        assert not any(k.startswith('_') for k in wire)
        assert json.loads(json.dumps(wire,allow_nan=False))['coordinateSystem']=='bd09ll'
    asyncio.run(run())


def test_invalid_final_mesh_cannot_publish_faces(monkeypatch):
    from tools import endpoint_boundary_surface as surface
    monkeypatch.setattr(surface,'preserved_network',lambda *a,**k:dict(status='invalid_seed',nodes={},all_triangles=[]))
    async def run():
        with pytest.raises(ValueError,match='Invalid final'):
            await compute_boundary_surface(IsochroneRequest(ORIGIN,'bd09ll',budget=200),Provider(),CancelToken())
    asyncio.run(run())


def test_live_request_uses_2400m_domain_and_84_initial_queries():
    from tools.endpoint_boundary_surface_live import make_request
    from life_circle.mesh import Mesh
    request = make_request()
    assert request.extent == request.max_extent == 1200
    assert request.expand is False
    assert (request.coarse_size, request.min_size) == (400, 50)
    points = Mesh(request.extent, request.coarse_size).required_points()
    assert len([p for p in points if p != (0, 0)]) == 84
    assert all(max(map(abs, p)) <= 1200 for p in points)


@pytest.mark.parametrize('xy', [(1201, 0), (-1201, 0), (0, 1201), (0, -1201)])
def test_live_guard_rejects_old_outer_band_before_counting(tmp_path, xy):
    from tools.endpoint_boundary_surface_live import SurfaceLedger
    from tools.live_smoke import LiveGuardError
    from life_circle.coordinates import LocalProjection, normalize
    from test_endpoint_live import request
    with SurfaceLedger(tmp_path) as ledger:
        ledger.arm()
        destination = normalize(LocalProjection(ORIGIN).to_geographic(xy))
        with pytest.raises(LiveGuardError, match='outside_fixed_request_domain'):
            ledger.reserve('analysis', request(destination), 1)
        assert ledger.data['counts']['analysis'] == 0


def test_2400m_boundary_points_survive_coordinate_rounding(tmp_path):
    from tools.endpoint_boundary_surface_live import SurfaceLedger, make_request
    from life_circle.mesh import Mesh
    from life_circle.coordinates import LocalProjection, normalize
    from test_endpoint_live import request
    profile = make_request()
    with SurfaceLedger(tmp_path) as ledger:
        ledger.arm()
        points = [p for p in Mesh(profile.extent, profile.coarse_size).required_points() if p != (0, 0)]
        for i, xy in enumerate(points):
            destination = normalize(LocalProjection(ORIGIN).to_geographic(xy))
            ledger.reserve('analysis', request(destination), i)
        assert ledger.data['counts']['analysis'] == 84


def test_2400m_surface_keeps_all_phases_and_geometry_in_domain():
    from dataclasses import replace
    from tools.endpoint_boundary_surface_live import make_request
    from life_circle.coordinates import LocalProjection
    from shapely.geometry import shape
    async def run():
        profile = replace(make_request(), budget=200, qps=None)
        provider = Provider(lambda x, y: (x*x+y*y)**.5/1.2)
        result = await compute_boundary_surface(profile, provider, CancelToken())
        projection = LocalProjection(ORIGIN)
        assert result['boundaryModel']['phases']['initial'] == 84
        assert 84 <= provider.calls <= 200
        assert all(max(map(abs, projection.to_local(o.destination))) <= 1200.1
                   for o in result['_observations'])
        domain = shape(result['calculationExtent'])
        assert shape(result['geometry']).difference(domain).area < 1e-18
        for lng, lat in domain.geoms[0].exterior.coords:
            assert max(map(abs, projection.to_local((lng, lat)))) == pytest.approx(1200)
    asyncio.run(run())
