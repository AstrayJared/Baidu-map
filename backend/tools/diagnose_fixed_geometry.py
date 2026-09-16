"""D2: freeze triangulated evidence; vary only raster resolution, without sampling."""
import argparse
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import shapely
from shapely.geometry import Polygon, mapping

from life_circle.field import reconstruct
from life_circle.scenarios import TestRoadNetwork
from tools.diagnostic_common import (read_json,write_json,write_csv,RunLedger,DiagnosticStop,
    require_development,no_network,verify_inputs,runtime_manifest)
from tools.offline_accuracy import predict,confusion,layout_definitions
from tools.audit_adaptive_budget import sha


def triangle_digest(state):
    return hashlib.sha256(json.dumps(state['triangles'],sort_keys=True,separators=(',',':')).encode()).hexdigest()


class FixedTriangles:
    def __init__(self,state):
        self.extent=state['extent']
        self.rows=[(Polygon(t['vertices']),t['vertices'],t['values']) for t in state['triangles']]

    def triangles(self):
        return self.rows


def fixed_rebuild(state,size):
    if size not in (25,12.5,5):
        raise DiagnosticStop('Unfrozen diagnostic raster size')
    return reconstruct(FixedTriangles(state),size)


def field_prediction(field,xx,yy):
    return predict(SimpleNamespace(local_geometry=field.geometry,local_unknown=field.unknown),xx,yy)


def direct_values(state,points):
    """Last matching VALID raw triangle, matching reconstruct's assignment order."""
    triangles=state['triangles']
    values=np.full(len(points),np.nan)
    identifiers=np.full(len(points),-1,dtype=int)
    weights=np.full((len(points),3),np.nan)
    valid=[i for i,t in enumerate(triangles) if all(v is not None for v in t['values'])]
    if not valid:
        return values,identifiers,weights
    tree=shapely.STRtree([Polygon(triangles[i]['vertices']) for i in valid])
    matches=tree.query(shapely.points(points),predicate='intersects')
    # Stable ordering explicitly follows raw triangle ordinal, not STRtree order.
    order=np.argsort(matches[1],kind='stable')
    pi,ti=matches[:,order]
    tids=np.asarray(valid)[ti]
    vertices=np.asarray([triangles[i]['vertices'] for i in tids])
    a,b,c=vertices[:,0],vertices[:,1],vertices[:,2]
    p=np.asarray(points)[pi]
    den=(b[:,1]-c[:,1])*(a[:,0]-c[:,0])+(c[:,0]-b[:,0])*(a[:,1]-c[:,1])
    u=((b[:,1]-c[:,1])*(p[:,0]-c[:,0])+(c[:,0]-b[:,0])*(p[:,1]-c[:,1]))/den
    v=((c[:,1]-a[:,1])*(p[:,0]-c[:,0])+(a[:,0]-c[:,0])*(p[:,1]-c[:,1]))/den
    w=np.column_stack((u,v,1-u-v))
    ts=np.asarray([triangles[i]['values'] for i in tids])
    values[pi]=np.sum(w*ts,axis=1);identifiers[pi]=tids;weights[pi]=w
    return values,identifiers,weights


def transitions(positive,before,after):
    positive,before,after=map(np.asarray,(positive,before,after))
    return dict(order=[-1,0,1],rows='before',columns='after',
        **{name:[[int(np.sum(mask&(before==a)&(after==b))) for b in (-1,0,1)] for a in (-1,0,1)]
           for name,mask in (('positive',positive),('negative',~positive))})


def check_detour_point():
    definition=next(d for d in layout_definitions() if d['id']=='detours-1')
    x,y=-120.,-140.
    c,s=np.cos(definition['angle']),np.sin(definition['angle'])
    u,v=c*x+s*y,-s*x+c*y
    network=TestRoadNetwork('wall')
    i,j=np.rint((np.array([u,v])+1600)/50).astype(int)
    node=[float(network.axis[i]),float(network.axis[j])]
    graph=float(network.distances[j,i])
    connector=float(np.hypot(u-node[0],v-node[1]))
    # Hand route: origin -> (950,0) outside wall: 950+900;
    # return inside to (200,50): 750+50; only (950,0)<->(900,0) enters.
    hand=950+900+750+50
    duration=(hand+connector)/1.2
    return dict(passed=node==[200.,50.] and graph==hand and abs(float(network(u,v))-duration)<1e-9,
        local_point=[x,y],rotated_point=[float(u),float(v)],mapped_node=node,
        distance_graph_m=graph,hand_distance_m=hand,connector_m=connector,duration_seconds=duration,
        truth_semantics='nearest 50m node + Euclidean connector; connector is not barrier checked',
        limitation='self-consistent synthetic discontinuity; not physically validated pedestrian truth')


def explain_errors(state,field,axis,truth,prediction,raw,ids,weights,hits):
    xx,yy=np.meshgrid(axis,axis)
    points=np.column_stack((xx.ravel(),yy.ravel()))
    positive=truth.ravel()<=900
    prediction=prediction.ravel()
    errors=(positive&(prediction!=1))|(~positive&(prediction==1))
    rows=[]
    for index in np.flatnonzero(errors):
        x,y=points[index]
        i,j=np.clip(np.searchsorted(field.x,[x,y],side='right')-1,0,len(field.x)-2)
        corners=field.z[j:j+2,i:i+2].ravel()
        raw_value=raw[index]
        raw_class=-1 if not np.isfinite(raw_value) else int(raw_value<=900)
        raw_wrong=raw_class!=-1 and bool(raw_class)!=bool(positive[index])
        raster_changed=int(prediction[index])!=raw_class
        no_hit=False;hit_wrong=False
        for h in hits:
            if h['bounds'][0]<=x<=h['bounds'][2] and h['bounds'][1]<=y<=h['bounds'][3]:
                no_hit |= h['positive_hits']==0
                hit_wrong |= h['positive_hits']>0
        labels=[]
        if no_hit: labels.append('component_no_positive_hit')
        if hit_wrong: labels.append('component_hit_representation_error')
        if raw_wrong: labels.append('raw_triangle_wrong_side')
        if raster_changed: labels.append('raster_or_support_changed_class')
        if not labels: labels.append('unassigned')
        # Primary mutually exclusive attribution. Overlapping labels above are not additive.
        primary=('no_raw_valid_support' if raw_class==-1 else
            'no_component_hit' if no_hit else 'raw_triangle_wrong_side' if raw_wrong else
            'raster_or_support' if raster_changed else 'unassigned')
        rows.append(dict(index=int(index),point=[float(x),float(y)],truth=float(truth.ravel()[index]),
            prediction=int(prediction[index]),kind='FP' if not positive[index] else 'FN_unknown' if prediction[index]==-1 else 'FN_known',
            triangle_id=int(ids[index]),barycentric=[float(v) if np.isfinite(v) else None for v in weights[index]],
            raw_time=float(raw_value) if np.isfinite(raw_value) else None,raw_class=raw_class,
            raster_cell=[int(i),int(j)],raster_corners=[float(v) if np.isfinite(v) else None for v in corners],
            cell_masked=bool(np.any(~np.isfinite(corners))),labels=labels,primary=primary))
    return rows


def run_d2(old,output):
    ledger=RunLedger.open(output)
    audit=read_json(output/'D0.json');verify_inputs(audit)
    d1=read_json(output/'D1/summary.json')
    if len(d1)!=40 or not all(all(r['checks'].values()) for r in d1):
        raise DiagnosticStop('D1 incomplete')
    directory=output/'D2';directory.mkdir(exist_ok=False)
    write_json(directory/'runtime.json',runtime_manifest())
    sanity=check_detour_point();write_json(directory/'detour-model-check.json',sanity)
    if not sanity['passed']:
        raise DiagnosticStop('Frozen model sanity check failed')
    rows=[]
    for run in d1:
        if run['budget']!=800:
            continue
        identity=require_development(run['layout'])
        stem=f"{identity}-800-{run['method']}"
        saved=read_json(output/f'D1/{stem}.json');state=saved['state']
        with np.load(old/f'{identity}-evaluation.npz') as ref:
            axis,truth=ref['axis'],ref['truth']
        with np.load(old/f'development/{stem}-predictions.npz') as pred:
            original=pred['prediction']
        xx,yy=np.meshgrid(axis,axis)
        raw,ids,weights=direct_values(state,np.column_stack((xx.ravel(),yy.ravel())))
        digest=triangle_digest(state)
        raw_support=shapely.union_all([poly for poly,_,v in FixedTriangles(state).rows if all(t is not None for t in v)])
        for size in (25,12.5,5):
            name=f'D2-{stem}-{size:g}'
            ledger.start('D2',name,1)
            ledger.consume('D2',name,dict(state_sha256=sha(output/f'D1/{stem}.json'),raster_size=size))
            started=time.perf_counter()
            try:
                field=fixed_rebuild(state,size)
                prediction=field_prediction(field,xx,yy)
                if triangle_digest(state)!=digest or (size==25 and not np.array_equal(prediction,original)):
                    raise DiagnosticStop('Evidence mutation or original raster mismatch')
                errors=explain_errors(state,field,axis,truth,prediction,raw,ids,weights,saved['component_hits'])
                row=dict(layout=identity,method=run['method'],raster_size=size,**confusion(truth<=900,prediction),
                    unknown_area_fraction=field.unknown.area/(3200**2),raw_support_area=raw_support.area,
                    triangle_sha256=digest,raw_support_sha256=hashlib.sha256(raw_support.wkb).hexdigest(),
                    elapsed_seconds=time.perf_counter()-started,sampling_attempts=0,
                    primary={kind:{key:sum(e['kind']==kind and e['primary']==key for e in errors)
                        for key in sorted({e['primary'] for e in errors})} for kind in ('FP','FN_known','FN_unknown')},
                    transitions=transitions(truth<=900,original,prediction))
                write_json(directory/f'{name}-errors.json',errors)
                write_json(directory/f'{name}-geometry.json',dict(geometry=mapping(field.geometry),unknown=mapping(field.unknown)))
                np.savez_compressed(directory/f'{name}-predictions.npz',prediction=prediction)
                ledger.finish(name,'completed')
            except BaseException:
                ledger.finish(name,'failed');raise
            rows.append(row);write_json(directory/'metrics.json',rows)
        print(json.dumps(dict(stage='D2',layout=identity,method=run['method'],reconstructions=3)),flush=True)
    write_csv(directory/'metrics.csv',rows)
    verify_inputs(audit)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with no_network():
        run_d2(args.old,args.output)


if __name__=='__main__': main()
