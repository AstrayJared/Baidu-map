"""Frozen B/L/P representation diagnostic. No sampling, oracle, or production edits."""
import argparse
import csv
import hashlib
import json
import multiprocessing
import subprocess
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import numpy as np
import shapely
from shapely.geometry import GeometryCollection, LineString, Point, Polygon, box, mapping, shape

from life_circle.field import FieldResult, multipolygon, reconstruct
from tools.diagnose_fixed_geometry import FixedTriangles, field_prediction
from tools.diagnostic_common import (DiagnosticStop, RunLedger, no_network, read_json,
                                     runtime_manifest, verify_inputs, write_csv, write_json)
from tools.local_probe_feasibility import (split_state, raw_support, compare_representation,
                                          score, outcome, e2_gate)
from tools.offline_accuracy import ROOT
from tools.audit_adaptive_budget import sha

LAYOUTS = ('jump-0', 'detours-1', 'jump-1')
METHODS = ('direct', 'raster12_5')
IDENTITIES = tuple(f'{m}-{n}-{v}' for m in METHODS for n in LAYOUTS for v in ('B','L','P'))
OLD = Path('D:/CodexOutputs/guodingyi-local-probe-feasibility-v1/run-01')
OUTPUT = Path('D:/CodexOutputs/guodingyi-fixed-evidence-representation-v1/run-01')
PLAN = ROOT/'backend/docs/国定一社区_固定证据表示层实验计划.md'
PLAN_HASH = '1370905dc9045ae86a1c894a495126b7f49bd2c3a1d3ce4064c0b83e21deb7dc'
BASELINE = 'c2fd23a274b482b6379bcd6779fd0f695adc4eb0'


class SupportIncompatible(ValueError):
    pass


@dataclass
class Product:
    field: FieldResult
    raw_support: object
    pieces: list
    degenerates: list


def edge_crossing(a, ta, b, tb):
    # Canonical order gives bit-identical shared edge intersections.
    a, b = tuple(map(float,a)), tuple(map(float,b))
    if b < a:
        a, b, ta, tb = b, a, tb, ta
    if ta == 900:
        return a
    if tb == 900:
        return b
    if ta == tb or not min(ta,tb) < 900 < max(ta,tb):
        raise ValueError('Edge does not strictly cross threshold')
    fraction = (900-ta)/(tb-ta)
    return tuple(a[i]+fraction*(b[i]-a[i]) for i in (0,1))


def clip_triangle(vertices, values):
    vertices = [tuple(map(float,p)) for p in vertices]
    if len(vertices)!=3 or len(values)!=3 or not np.isfinite(vertices).all() or not np.isfinite(values).all() or min(values)<0:
        raise ValueError('Invalid triangle observation')
    parent = Polygon(vertices)
    if not parent.is_valid or parent.area<=0:
        raise ValueError('Degenerate source triangle')
    points=[]
    for i,j in ((0,1),(1,2),(2,0)):
        a,b,ta,tb=vertices[i],vertices[j],values[i],values[j]
        if ta<=900:
            points.append(a)
        if min(ta,tb)<900<max(ta,tb):
            points.append(edge_crossing(a,ta,b,tb))
    points=list(dict.fromkeys(points))
    if not points:
        return GeometryCollection()
    if len(points)==1:
        return Point(points[0])
    if len(points)==2:
        return LineString(points)
    result=Polygon(points)
    if not result.is_valid or result.area<=0:
        raise ValueError('Invalid clipped polygon; no repair allowed')
    return result


def generate(state, method):
    if method not in METHODS:
        raise DiagnosticStop('Unregistered representation')
    support_parts=[]
    pieces=[]
    degenerates=[]
    for ordinal,t in enumerate(state['triangles']):
        vertices,values=t['vertices'],t['values']
        if any(v is None for v in values):
            continue
        if not np.isfinite(values).all() or min(values)<0:
            raise ValueError('Nonfinite observation')
        parent=Polygon(vertices)
        if not parent.is_valid or parent.area<=0:
            raise ValueError('Invalid input triangle')
        support_parts.append(parent)
        if method=='direct':
            clipped=clip_triangle(vertices,values)
            if clipped.geom_type=='Polygon':
                pieces.append(dict(ordinal=ordinal,geometry=mapping(clipped)))
            elif not clipped.is_empty:
                degenerates.append(dict(ordinal=ordinal,geometry=mapping(clipped)))
    support=shapely.union_all(support_parts)
    if not support.is_valid:
        raise ValueError('Invalid raw support')
    if method=='direct':
        geometry=multipolygon(shapely.union_all([shape(p['geometry']) for p in pieces]))
        if not geometry.is_valid:
            raise ValueError('Invalid union; no repair allowed')
        domain=box(-state['extent'],-state['extent'],state['extent'],state['extent'])
        field=FieldResult(geometry,domain.difference(support),support,np.array([]),np.empty((0,0)))
    else:
        field=reconstruct(FixedTriangles(state),12.5)
    return Product(field,support,pieces,degenerates)


def prediction(field, axis):
    if field is None:
        return np.full((len(axis),len(axis)),-1,dtype=np.int8)
    return field_prediction(field,*np.meshgrid(axis,axis))


def restrict(product,s0,u0,axis,extent):
    domain=box(-extent,-extent,extent,extent)
    tol=1e-8*domain.area
    xx,yy=np.meshgrid(axis,axis)
    points=shapely.points(xx,yy)
    common=s0.intersection(product.field.support)
    natural_unknown=domain.difference(common)
    missing=s0.difference(product.field.support).area
    unknown_mismatch=int(np.sum(shapely.covers(natural_unknown,points)!=shapely.covers(u0,points)))
    checks=dict(missing_support_area=missing,unknown_reference_mismatches=unknown_mismatch,
                u0_complement_area=u0.symmetric_difference(domain.difference(s0)).area,
                raw_missing_support_area=s0.difference(product.raw_support).area)
    if max(missing,checks['u0_complement_area'],checks['raw_missing_support_area'])>tol or unknown_mismatch:
        raise SupportIncompatible('Natural representation cannot cover frozen support')
    geometry=multipolygon(product.field.geometry.intersection(s0))
    if not geometry.is_valid:
        raise ValueError('Invalid restricted polygon')
    return FieldResult(geometry,u0,s0,product.field.x,product.field.z),checks


def field_json(field):
    return {k:mapping(getattr(field,k)) for k in ('geometry','support','unknown')}


def restored_field(row):
    if row is None:
        return None
    return FieldResult(*(shape(row[k]) for k in ('geometry','unknown','support')),np.array([]),np.empty((0,0)))


class OutputLedger(RunLedger):
    cancelled=False

    @classmethod
    def create(cls,directory):
        return super().create(directory,dict(direct=9,raster12_5=9))

    def save(self):
        if self.broken:
            raise DiagnosticStop('Ledger permanently failed')
        try:
            write_json(self.directory/'ledger.json',self.data)
        except Exception:
            self.broken=True
            raise DiagnosticStop('Ledger persistence failed') from None


def run_reserved(ledger,identity,execute,persist):
    if ledger.cancelled or ledger.broken or identity not in IDENTITIES:
        raise DiagnosticStop('Cancelled, failed, or unregistered output')
    method=identity.split('-')[0]
    ledger.start(method,identity,1)
    ledger.consume(method,identity,dict(event='global_output_reserved'))
    started=time.perf_counter()
    try:
        result=execute()
        if ledger.cancelled:
            ledger.finish(identity,'cancelled',elapsed_seconds=time.perf_counter()-started)
            raise DiagnosticStop('Late generation after cancellation rejected')
        try:
            persist(result)
        except Exception:
            ledger.broken=True
            raise DiagnosticStop('Output persistence failed') from None
        ledger.finish(identity,'completed',elapsed_seconds=time.perf_counter()-started)
        return result
    except (DiagnosticStop,KeyboardInterrupt) as exc:
        if isinstance(exc,KeyboardInterrupt):
            ledger.cancelled=True
        if not ledger.broken and ledger.data['runs'][identity]['status']=='running':
            ledger.finish(identity,'cancelled' if ledger.cancelled else 'blocked',elapsed_seconds=time.perf_counter()-started)
        raise
    except Exception as exc:
        ledger.finish(identity,'failed',elapsed_seconds=time.perf_counter()-started,error_type=type(exc).__name__)
        raise


@contextmanager
def offline_only():
    def blocked(*args,**kwargs):
        raise DiagnosticStop('Oracle or sampling is forbidden')
    with no_network(), ExitStack() as stack:
        for target in ('life_circle.scenarios.TestRoadNetwork.__init__',
                       'tools.connector_sensitivity.ConnectorModel.evaluate',
                       'life_circle.engine.compute_isochrone','life_circle.scheduler.Scheduler.observe_many'):
            stack.enter_context(patch(target,blocked))
        yield


def _worker(state,method,directory,connection):
    try:
        with offline_only():
            started=time.perf_counter()
            product=generate(state,method)
            elapsed=time.perf_counter()-started
            try:
                directory.mkdir(exist_ok=False)
                np.savez_compressed(directory/'raster.npz',x=product.field.x,z=product.field.z)
                write_json(directory/'natural.json',dict(field=field_json(product.field),raw_support=mapping(product.raw_support),
                    pieces=product.pieces,degenerates=product.degenerates,generation_seconds=elapsed))
            except Exception:
                raise DiagnosticStop('Worker persistence failed') from None
            connection.send(('ok',None))
    except DiagnosticStop:
        connection.send(('blocked',None))
    except Exception as exc:
        connection.send(('failed',type(exc).__name__))
    finally:
        connection.close()


def generate_process(state,method,directory,cancelled=lambda:False,timeout=600):
    ctx=multiprocessing.get_context('spawn')
    receiver,sender=ctx.Pipe(duplex=False)
    process=ctx.Process(target=_worker,args=(state,method,directory,sender),daemon=True)
    started=time.monotonic()
    try:
        if cancelled():
            raise DiagnosticStop('Cancelled before worker start')
        process.start()
        sender.close()
        while not receiver.poll(.05):
            if cancelled():
                raise DiagnosticStop('Cancelled worker')
            if time.monotonic()-started>=timeout:
                raise TimeoutError('Output deadline reached')
            if not process.is_alive():
                raise DiagnosticStop('Worker exited without auditable result')
        if time.monotonic()-started>=timeout:
            raise TimeoutError('Late output rejected at deadline')
        status,reason=receiver.recv()
        if cancelled():
            raise DiagnosticStop('Late worker rejected')
        if status=='blocked':
            raise DiagnosticStop('Worker protection failed')
        if status!='ok':
            raise ValueError('Worker generation failed: '+str(reason))
        process.join(timeout=2)
        if process.is_alive():
            raise DiagnosticStop('Worker did not terminate')
        if time.monotonic()-started>=timeout:
            raise TimeoutError('Output deadline reached during worker exit')
        row=read_json(directory/'natural.json')
        with np.load(directory/'raster.npz') as raster:
            field=restored_field(row['field'])
            field.x,field.z=raster['x'].copy(),raster['z'].copy()
        return Product(field,shape(row['raw_support']),row['pieces'],row['degenerates'])
    finally:
        if process.pid is not None and process.is_alive():
            process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill();process.join()
        receiver.close();sender.close()


def execute_matrix(build,control):
    results={}
    for method in METHODS:
        result=dict(status='running',outputs={},checks={})
        results[method]=result
        try:
            for name in LAYOUTS:
                b=build(method,name,'B');result['outputs'][name+'-B']=b
                l=build(method,name,'L');result['outputs'][name+'-L']=l
                check=control(method,name,b,l);result['checks'][name]=check
                if not check['passed']:
                    result['status']='representation_invalid'
                    break
            if result['status']!='running':
                continue
            for name in LAYOUTS:
                result['outputs'][name+'-P']=build(method,name,'P')
            result['status']='completed'
        except SupportIncompatible:
            result['status']='support_incompatible'
        except Exception as exc:
            result.update(status='failed',error_type=type(exc).__name__)
    return results


def transfer(before,after,selected,axis,truth,extent):
    delta=before.geometry.symmetric_difference(after.geometry)
    xx,yy=np.meshgrid(axis,axis);points=shapely.points(xx,yy)
    valid=np.isfinite(truth)
    before_pred,after_pred=prediction(before,axis),prediction(after,axis)
    changes=valid&(before_pred!=after_pred)
    parents=[Polygon(p['vertices']) for p in selected]
    q=shapely.union_all(parents)
    areas=[delta.intersection(p).area for p in parents]
    transferred=bool(delta.area>1e-8*(2*extent)**2 or any(a>1e-8*p.area for a,p in zip(areas,parents)) or changes.any())
    hit=valid&shapely.covers(delta,points)
    q_refs=valid&shapely.covers(q.intersection(before.support),points)
    local=[]
    for p,area in zip(parents,areas):
        interior=valid&shapely.contains(p,points)
        boundary=valid&shapely.covers(p.boundary,points)
        local.append(dict(area=area,valid_interior=int(interior.sum()),valid_boundary=int(boundary.sum()),
            positive_interior=int((interior&(truth<=900)).sum()),negative_interior=int((interior&(truth>900)).sum()),
            severe_negative_interior=int((interior&(truth>1020)).sum())))
    guards=outcome(truth,before_pred,after_pred)
    evidence=('guardrail_failed' if guards['guardrail_failed'] else 'insufficient' if transferred and not changes.any() else 'not_supported')
    return dict(evidence_transferred=transferred,accuracy_evidence=evidence,delta_area=delta.area,
        outside_parent_delta_area=delta.difference(q).area,delta_valid_reference_count=int(hit.sum()),
        parent_support_valid_references=int(q_refs.sum()),
        parent_support_positive_references=int((q_refs&(truth<=900)).sum()),
        parent_support_negative_references=int((q_refs&(truth>900)).sum()),
        changed_reference_count=int(changes.sum()),changed_reference_inside_parents=int((changes&shapely.covers(q,points)).sum()),
        changed_reference_outside_parents=int((changes&~shapely.covers(q,points)).sum()),parents=local,paired=guards)


def method_verdict(paired,legacy,compatible=True,transfers=None):
    if not compatible:
        return dict(accuracy_evidence='not_evaluated',candidate_design_supported=False)
    gate=e2_gate(paired,True)
    legacy_failed=any(r['guardrail_failed'] for r in legacy.values())
    evidence='guardrail_failed' if legacy_failed else gate
    if transfers and gate=='mechanism_not_supported' and not legacy_failed and any(t['evidence_transferred'] for t in transfers.values()) and not any(t['changed_reference_count'] for t in transfers.values()):
        evidence='insufficient'
    return dict(accuracy_evidence=evidence,paired_gate=gate,legacy_guardrails_passed=not legacy_failed,
        candidate_design_supported=evidence=='accuracy_candidate_evidence')


def digest_json(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def verify_frozen_manifest(path):
    frozen=json.loads(subprocess.check_output(['git','show',BASELINE+':backend/docs/国定一社区_E0-E2冻结清单.json'],cwd=ROOT).decode('utf-8'))
    current=read_json(path)
    if current!=frozen:
        raise DiagnosticStop('Historical manifest differs from frozen Git baseline')
    return current


def write_changes(path,rows):
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,['index','x','y','truth','before','after'])
        writer.writeheader();writer.writerows(rows)


def freeze_inputs():
    if sha(PLAN)!=PLAN_HASH:
        raise DiagnosticStop('Approved plan differs')
    if subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip():
        raise DiagnosticStop('Commit tools and plan before formal execution')
    if subprocess.check_output(['git','diff',BASELINE,'--name-only','--','life-circle-algorithm','backend/app','frontend'],cwd=ROOT,text=True).strip():
        raise DiagnosticStop('Protected production code changed')
    changed=subprocess.check_output(['git','diff',BASELINE,'--name-only','--','backend/tools'],cwd=ROOT,text=True).splitlines()
    if any(p!='backend/tools/fixed_evidence_representation.py' for p in changed):
        raise DiagnosticStop('Existing experimental tools changed')
    manifest_path=ROOT/'backend/docs/国定一社区_E0-E2冻结清单.json'
    manifest=verify_frozen_manifest(manifest_path)
    inputs={str(PLAN):PLAN_HASH,str(manifest_path):sha(manifest_path)}
    expected={'protocol.json':manifest['protocol_sha256'],'reference-points.npz':manifest['reference_point_sha256'],
              'probe-selection.json':manifest['probe_selection_sha256']}
    for name in LAYOUTS:
        for suffix in (f'E0-{name}.json',f'E0-{name}-field.npz',f'linear-{name}.json',f'actual-{name}.json',
                       f'E2-{name}-observations.json',f'{name}-reference.npz'):
            expected[suffix]=manifest['formal_artifacts'][suffix]
    for name,h in expected.items():
        if sha(OLD/name)!=h:
            raise DiagnosticStop('Frozen evidence mismatch')
        inputs[str(OLD/name)]=h
    for directory in ('backend/tools','backend/tests','life-circle-algorithm/src'):
        for path in sorted((ROOT/directory).rglob('*.py')):
            inputs[str(path)]=sha(path)
    return dict(version='fixed-evidence-representation-v1',inputs=inputs,runtime=runtime_manifest(),
        methods=list(METHODS),layouts=list(LAYOUTS),identities=list(IDENTITIES),oracle_cap=0,baidu_cap=0,
        output_cap=18,output_deadline_seconds=600,old_root=str(OLD),sealed_predictions=0)


def load_evidence(old=OLD):
    selections=read_json(old/'probe-selection.json')
    evidence={}
    for name in LAYOUTS:
        saved=read_json(old/f'E0-{name}.json')
        state=saved['state'];selected=selections[name]['points']
        observed=read_json(old/f'E2-{name}-observations.json')
        if len(observed)!=len(selected) or any(not o['endpoint_verified'] or o['duration'] is None or o['destination']!=p['destination'] for p,o in zip(selected,observed)):
            raise DiagnosticStop('Probe evidence does not match fixed selection')
        states=dict(B=state,L=split_state(state,selected,[p['linear_duration'] for p in selected]),
                    P=split_state(state,selected,[o['duration'] for o in observed]))
        # Geometry audit only, not threshold reconstruction or new prediction.
        raw=raw_support(state)
        if any(raw.symmetric_difference(raw_support(s)).area>1e-8*(2*state['extent'])**2 for s in states.values()):
            raise DiagnosticStop('Evidence states have different raw support')
        with np.load(old/f'{name}-reference.npz') as ref:
            axis,truth=ref['axis'].copy(),ref['truth'].copy()
        anchors=dict(B=saved['field'],L=read_json(old/f'linear-{name}.json')['field'],P=read_json(old/f'actual-{name}.json')['field'])
        evidence[name]=dict(states=states,selected=selected,axis=axis,truth=truth,anchors=anchors,
            s0=shape(saved['field']['support']),u0=shape(saved['field']['unknown']))
    return evidence


def execute(output=OUTPUT):
    protocol=freeze_inputs()
    evidence=load_evidence()
    ledger=OutputLedger.create(output)
    try:
        write_json(output/'protocol.json',protocol)
        write_json(output/'protocol.sha256.json',dict(sha256=sha(output/'protocol.json')))
        for name,e in evidence.items():
            write_json(output/f'{name}-states.json',e['states'])
        state_hashes={name:{v:digest_json(s) for v,s in e['states'].items()} for name,e in evidence.items()}
        write_json(output/'state-hashes.json',state_hashes)
        def build(method,name,variant):
            verify_inputs(protocol)
            e=evidence[name];state=e['states'][variant]
            if digest_json(state)!=state_hashes[name][variant]:
                raise DiagnosticStop('In-memory evidence changed')
            identity=f'{method}-{name}-{variant}'
            def compute():
                started=time.monotonic()
                product=generate_process(state,method,output/identity,cancelled=lambda:ledger.cancelled)
                verify_inputs(protocol)
                field,checks=restrict(product,e['s0'],e['u0'],e['axis'],state['extent'])
                if time.monotonic()-started>=600:
                    raise TimeoutError('Named output validation exceeded deadline')
                return dict(field=field_json(field),support_checks=checks,raw_support=mapping(product.raw_support),
                    clipped_area=product.field.geometry.difference(field.geometry).area)
            return run_reserved(ledger,identity,compute,lambda row:write_json(output/f'{identity}.json',row))
        def control(method,name,b,l):
            e=evidence[name]
            checks=compare_representation(e['states']['B'],e['states']['L'],e['selected'],
                restored_field(b['field']),restored_field(l['field']),e['axis'])
            try:
                write_json(output/f'{method}-{name}-control.json',checks)
            except Exception:
                ledger.broken=True
                raise DiagnosticStop('Control persistence failed') from None
            return checks
        matrix=execute_matrix(build,control)
        verify_inputs(protocol)
        write_json(output/'matrix.json',matrix)
        write_json(output/'execution.json',{identity:ledger.data['runs'].get(identity,dict(status='not_executed',reason=matrix[identity.split('-')[0]]['status'])) for identity in IDENTITIES})
        summarize(output,evidence,matrix)
    except BaseException:
        # No fallback experiment or restart; keep the durable started marker.
        ledger.cancelled=True
        raise


def summarize(output,evidence=None,matrix=None):
    evidence=evidence or load_evidence()
    matrix=matrix or read_json(output/'matrix.json')
    metrics=[];methods={};all_transitions={}
    for method in ('saved25',*METHODS):
        paired={};legacy={};transfers={}
        compatible=method=='saved25' or matrix[method]['status']=='completed'
        for name,e in evidence.items():
            axis,truth=e['axis'],e['truth'];xx,yy=np.meshgrid(axis,axis)
            anchors={v:restored_field(e['anchors'][v]) for v in ('B','L','P')}
            fields={}
            for variant in ('B','L','P'):
                row=e['anchors'][variant] if method=='saved25' else matrix[method]['outputs'].get(name+'-'+variant,{}).get('field')
                field=restored_field(row);fields[variant]=field
                pred=prediction(field,axis)
                identity=f'{method}-{name}-{variant}'
                execution=read_json(output/'ledger.json')['runs'].get(identity,{}) if method!='saved25' else {}
                metric=dict(method=method,layout=name,variant=variant,status='saved' if method=='saved25' else 'completed' if row else 'not_available',
                    method_status='saved' if method=='saved25' else matrix[method]['status'],**score(truth,pred),
                    components=0 if field is None else len(multipolygon(field.geometry).geoms),
                    holes=0 if field is None else sum(len(g.interiors) for g in multipolygon(field.geometry).geoms),
                    unknown_area=(2*e['states']['B']['extent'])**2 if field is None else field.unknown.area,
                    output_reserved=execution.get('used',0),wall_seconds=execution.get('elapsed_seconds'),
                    new_requests=0,severe_fp_ids=np.flatnonzero((np.isfinite(truth)&(truth>1020)&(pred==1)).ravel()).tolist(),
                    fp_above_960_ids=np.flatnonzero((np.isfinite(truth)&(truth>960)&(pred==1)).ravel()).tolist())
                metrics.append(metric)
                np.savez_compressed(output/f'{identity}-predictions.npz',prediction=pred)
            if method=='saved25':
                continue
            # Keep four comparisons, including failed/null methods. Never score a
            # failed branch as a zero-FP benefit or erase completed diagnostics.
            comparisons=dict(B_L=(fields['B'],fields['L']),L_P=(fields['L'],fields['P']),
                old_B=(anchors['B'],fields['B']),old_P=(anchors['B'],fields['P']))
            for label,(before,after) in comparisons.items():
                a,b=prediction(before,axis),prediction(after,axis)
                detail=outcome(truth,a,b)
                all_transitions[f'{method}-{name}-{label}']=detail
                changes=np.flatnonzero((np.isfinite(truth)&(a!=b)).ravel())
                rows=[dict(index=int(i),x=float(xx.ravel()[i]),y=float(yy.ravel()[i]),truth=float(truth.ravel()[i]),before=int(a.ravel()[i]),after=int(b.ravel()[i])) for i in changes]
                write_changes(output/f'{method}-{name}-{label}-changes.csv',rows)
            paired[name]=all_transitions[f'{method}-{name}-L_P']
            legacy[name]=all_transitions[f'{method}-{name}-old_P']
            if compatible:
                transfers[name]=transfer(fields['L'],fields['P'],e['selected'],axis,truth,e['states']['B']['extent'])
                natural_l=read_json(output/f'{method}-{name}-L/natural.json')['field']
                natural_p=read_json(output/f'{method}-{name}-P/natural.json')['field']
                delta=shape(natural_l['geometry']).symmetric_difference(shape(natural_p['geometry']))
                transfers[name].update(natural_delta_area=delta.area,delta_clipped_by_s0_area=delta.difference(e['s0']).area)
                if method=='direct' and transfers[name]['outside_parent_delta_area']>1e-8*(2*e['states']['B']['extent'])**2:
                    compatible=False
        if method!='saved25':
            methods[method]=dict(representation_status=matrix[method]['status'] if compatible else matrix[method]['status'] if matrix[method]['status']!='completed' else 'outside_parent_change',
                transfers=transfers,**method_verdict(paired,legacy,compatible,transfers))
    ledger=read_json(output/'ledger.json')
    summary=dict(version='fixed-evidence-representation-v1',methods=methods,new_requests=0,oracle_calls=0,baidu_calls=0,
        sealed_predictions=0,output_cap=18,outputs_reserved=sum(ledger['used'].values()),
        outputs_completed=sum(r['status']=='completed' for r in ledger['runs'].values()),
        candidate_design_supported=any(m['candidate_design_supported'] for m in methods.values()))
    write_json(output/'metrics.json',metrics);write_csv(output/'metrics.csv',metrics)
    write_json(output/'transitions.json',all_transitions);write_json(output/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,allow_nan=False))
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--summarize-only',action='store_true')
    args=parser.parse_args()
    with offline_only():
        if args.summarize_only:
            summarize(OUTPUT)
        else:
            execute()


if __name__=='__main__':
    main()
