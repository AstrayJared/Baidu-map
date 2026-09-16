import asyncio
import math
import pytest

from shapely.geometry import Point, box, shape

from life_circle.models import CancelToken
from test_endpoint_radial_boundary import request
from tools.endpoint_radial_boundary import compute_radial_boundary
from tools.endpoint_radial_experiment import Synthetic, local
from tools.endpoint_boundary_band import connect_estimate


def row(angle, radius=650, width=80, status='unfinished', committed=True):
    def record(r,t):
        return dict(xy=[r*math.cos(angle),r*math.sin(angle)],duration=t,origin=['same'],id=f'{angle}:{t}')
    a,b=record(radius-width/2,800),record(radius+width/2,1000)
    return dict(angle=angle,status=status,reason='offset_stagnation' if status!='localized' else None,
        committed=committed,bracket=dict(status=status,reason='offset_stagnation' if status!='localized' else None,
        left=a,right=b,width_m=width,midpoint_xy=[radius*math.cos(angle),radius*math.sin(angle)]))


def test_actual_angle_order_connects_shuffled_brackets_and_keeps_band_near_boundary():
    rows=[row(2*math.pi*i/16) for i in range(16)]
    result=connect_estimate(rows[::-1],(0,0),box(-1200,-1200,1200,1200),set())
    assert result['envelope'].is_valid and result['envelope'].covers(Point(0,0))
    assert not result['band'].covers(Point(0,0))
    assert all(s['width_m']==pytest.approx(80) for s in result['segments'])
    for r in rows:
        assert result['band'].buffer(1e-6).covers(Point(r['bracket']['left']['xy']))
        assert result['band'].buffer(1e-6).covers(Point(r['bracket']['right']['xy']))


def test_failed_new_direction_preserves_prior_estimate_and_marks_segment():
    rows=[row(2*math.pi*i/16,status='localized') for i in range(16)]
    before=connect_estimate(rows,(0,0),box(-1200,-1200,1200,1200),set())
    rows.append(dict(angle=.1,status='unknown',reason='budget',bracket=None,committed=False))
    after=connect_estimate(rows,(0,0),box(-1200,-1200,1200,1200),set())
    assert after['envelope'].equals(before['envelope'])
    assert any('unfinished_direction' in s['reasons'] for s in after['segments'])


def test_missing_opposite_evidence_and_conflicts_cannot_make_closed_ring():
    rows=[row(2*math.pi*i/4) for i in range(4)]
    bad={tuple(rows[0]['bracket']['left']['xy'])}
    assert connect_estimate(rows,(0,0),box(-1200,-1200,1200,1200),bad)['envelope'] is None
    assert connect_estimate([dict(angle=0,bracket=None)],(0,0),box(-1200,-1200,1200,1200),set())['envelope'] is None


def test_snap_returns_honest_estimate_and_bounded_neighbor_probes():
    async def run():
        p=Synthetic('snap80')
        r=await compute_radial_boundary(request(),p,CancelToken(),directions=16,boundary_bands=True)
        assert r['geometry'] is not None and r['uncertaintyBand'] is not None
        assert r['uncertainty']['guaranteedCoverage'] is False
        assert not local(r['uncertaintyBand']).covers(Point(0,0))
        assert all(d['status']!='localized' for d in r['directions'])
        assert all(len(d.get('sideProbes',[]))<=4 for d in r['directions'])
        assert r['calls']==p.calls<=400
        assert not any(e['kind']=='interior_check' for e in r['_evidence'])
    asyncio.run(run())


def test_cancel_publishes_neither_estimate_nor_band():
    async def run():
        token=CancelToken();token.cancel();p=Synthetic('circle')
        r=await compute_radial_boundary(request(),p,token,boundary_bands=True)
        assert r['geometry'] is None and r['uncertaintyBand'] is None and p.calls==0
    asyncio.run(run())


def test_neighbor_probe_shrinks_only_with_new_actual_opposite_pair():
    from test_endpoint_boundary import session
    from tools.endpoint_boundary_band import probe_sides
    async def run():
        s,p,_=session()
        a,_=await s.measure((-100,0),'initial');b,_=await s.measure((100,0),'initial')
        bracket=await s.refine(a,b,max_rounds=1)
        bracket['reason']='offset_stagnation'
        after,probes=await probe_sides(s,bracket,target=25)
        assert after['width_m'] < bracket['width_m'] and after['status']=='localized'
        assert 1<=len(probes)<=4 and any(p['accepted'] for p in probes)
        assert (after['left']['duration']<=900)!=(after['right']['duration']<=900)
    asyncio.run(run())


def test_band_mode_is_deterministic_and_obeys_small_budget():
    async def run():
        outputs=[]
        for _ in range(2):
            p=Synthetic('offset60')
            r=await compute_radial_boundary(request(100),p,CancelToken(),boundary_bands=True,adaptive=True)
            assert r['calls']==p.calls<=100
            outputs.append(r)
        assert outputs[0]['geometry']==outputs[1]['geometry']
        assert outputs[0]['uncertaintyBand']==outputs[1]['uncertaintyBand']
    asyncio.run(run())


def test_known_negative_is_preserved_and_blocks_conflicting_filled_result():
    rows=[row(2*math.pi*i/16,status='localized') for i in range(16)]
    witness=dict(id='measured-negative',xy=[300,0],duration=1900,origin=['same'])
    result=connect_estimate(rows,(0,0),box(-1200,-1200,1200,1200),set(),witnesses=[witness])
    assert result['envelope'] is None
    assert result['candidate'].covers(Point(300,0))
    assert result['reason']=='known_negative_inside_estimate'
    assert result['negative_evidence'][0]['id']=='measured-negative'
    assert result['negative_evidence'][0]['insideEstimate'] is True
    assert result['negative_evidence'][0]['physicalBarrierVerified'] is False
    assert any('measured-negative' in s.get('negativeEvidenceIds',[]) for s in result['segments'])


def test_negative_and_jump_evidence_survive_without_a_closed_ring():
    rows=[row(0,status='localized')]
    rows[0]['bracket']['suspected_jump']=True
    witness=dict(id='outside',xy=[1000,0],duration=1100,origin=['same'])
    result=connect_estimate(rows,(0,0),box(-1200,-1200,1200,1200),set(),witnesses=[witness])
    assert result['negative_evidence'][0]['insideEstimate'] is None
    assert result['jump_evidence'][0]['sourceIds']==[p['id'] for p in (rows[0]['bracket']['left'],rows[0]['bracket']['right'])]


def test_public_evidence_keeps_rejected_measurements_separate_from_negative_points():
    async def run():
        r=await compute_radial_boundary(request(),Synthetic('missing'),CancelToken(),directions=16,boundary_bands=True)
        assert len(r['observationEvidence'])==len(r['_evidence'])
        assert any(e.get('reason')=='no_route' for e in r['observationEvidence'])
        assert all(e['durationSeconds']>900 and e['kind']=='over_threshold' for e in r['negativeEvidence'])
        assert all(e['physicalBarrierVerified'] is False for e in r['negativeEvidence'])
    asyncio.run(run())
