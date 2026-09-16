"""E6: boundary-first refinement using measured actual endpoints, never reference labels.

Research bridge for the E5 endpoint contract and R1 spatial bisection. Output is
a local bracket, not an inferred complete two-dimensional walking polygon.
"""
from dataclasses import asdict
import math

from shapely.geometry import LineString, MultiPoint, Point
from life_circle.coordinates import LocalProjection, normalize
from tools.endpoint_preserved_triangulation import preserved_network


def region_decision(corners, center):
    if len(corners)<3 or any(r is None for r in corners) or center is None:
        return dict(status='unknown',reason='missing_region_observation')
    hull=MultiPoint([r['xy'] for r in corners]).convex_hull
    if hull.area==0 or not hull.contains(Point(center['xy'])):
        return dict(status='unknown',reason='center_not_inside_actual_vertices')
    labels={r['duration']<=900 for r in [*corners,center]}
    status='mixed' if len(labels)>1 else 'inside_assumed' if True in labels else 'outside_assumed'
    return dict(status=status,reason=None,assumption=status!='mixed',actual_support_wkt=hull.wkt)


class BoundarySession:
    def __init__(self,scheduler,domain):
        if not domain.is_valid or domain.area<=0:raise ValueError('Invalid boundary domain')
        self.scheduler=scheduler;self.domain=domain;self.projection=LocalProjection(scheduler.origin)
        self.origin=None;self.observations=[];self.log=[];self._points={};self._conflicts=set();self._requests={}
        # The center check must be a provider observation, not the engine's derived anchor.
        cached=scheduler.cache.get(scheduler.origin)
        if cached is not None and cached.attempts==0:scheduler.cache.pop(scheduler.origin)

    @property
    def records(self):
        return [v for k,v in self._points.items() if k not in self._conflicts]

    def ingest(self,obs,kind='import'):
        """Preserve raw evidence even if it cannot support the current bracket."""
        self.observations.append(obs)
        row={k:asdict(obs)[k] for k in ('destination','observed_duration','reason','route_origin','route_destination',
             'request_origin','endpoint_verified','attempts','destination_offset_m')}
        row.update(id=f'e6-{len(self.log):05d}',kind=kind,accepted=False);self.log.append(row)
        reason=None
        if obs.attempts==0:reason='not_provider_observation'
        elif obs.observed_duration is None or obs.reason not in (None,'endpoint_offset'):reason=obs.reason or 'invalid_measurement'
        elif not obs.endpoint_verified or obs.route_origin is None or obs.route_destination is None:reason='missing_endpoints'
        else:
            try:normalize(obs.route_origin);normalize(obs.route_destination)
            except (TypeError,ValueError):reason='invalid_endpoints'
        if reason:row['rejected']=reason;return None,reason
        origin=[list(obs.request_origin or self.scheduler.origin),list(obs.route_origin)]
        if self.origin is None:self.origin=origin
        if origin!=self.origin:row['rejected']='different_actual_origin';return None,'different_actual_origin'
        xy=tuple(self.projection.to_local(obs.route_destination));prior=self._points.get(xy)
        if xy in self._conflicts or (prior is not None and prior['duration']!=obs.observed_duration):
            self._conflicts.add(xy);row['rejected']='conflicting_observations';return None,'conflicting_observations'
        row['accepted']=True
        if prior is not None:
            # Reused spatial evidence still carries THIS response's requested
            # coordinate for displacement correction, not the first request's.
            return dict(prior,request_xy=list(self.projection.to_local(obs.destination))),None
        record=dict(id=row['id'],xy=list(xy),duration=obs.observed_duration,origin=origin,
                    request_xy=list(self.projection.to_local(obs.destination)))
        self._points[xy]=record
        return record,None

    async def measure(self,xy,kind):
        if self.scheduler.token.cancelled:return None,'cancelled'
        if self.scheduler.clock.time()>=self.scheduler.deadline:return None,'deadline'
        if not self.domain.covers(Point(xy)):return None,'request_outside_domain'
        target=normalize(self.projection.to_geographic(xy))
        if target in self._requests:
            record,reason=self._requests[target]
            if record and tuple(record['xy']) in self._conflicts:return None,'conflicting_observations'
            return record,reason
        if self.scheduler.remaining<=0:return None,'budget'
        obs=await self.scheduler.query(target)
        if self.scheduler.token.cancelled:return None,'cancelled'
        if self.scheduler.clock.time()>=self.scheduler.deadline:return None,'deadline'
        result=self.ingest(obs,kind);self._requests[target]=result
        return result

    def candidate_edges(self):
        """Unfiltered mesh discovers where queries are needed; it does not fill faces."""
        net=preserved_network(self.records,self.domain,radius=None)
        if net['status'].startswith('invalid_'):raise ValueError('Invalid endpoint discovery mesh')
        nodes=net['nodes'];by_id={r['id']:r for r in self.records};edges=set()
        for face in net['all_triangles']:
            for i,j in ((0,1),(1,2),(2,0)):
                a,b=face[i],face[j]
                if (nodes[a]['duration']<=900)==(nodes[b]['duration']<=900):continue
                line=LineString([nodes[a]['actual'],nodes[b]['actual']])
                if line.intersection(self.domain).length<=0:continue
                if any(line.distance(Point(xy))<1e-7 for xy in self._conflicts):continue
                # A failed requested location along an edge remains a support gap.
                if any(not r['accepted'] and line.distance(Point(self.projection.to_local(r['destination'])))<.08 for r in self.log):continue
                edges.add(tuple(sorted((nodes[a]['record_ids'][0],nodes[b]['record_ids'][0]))))
        return [(by_id[a],by_id[b]) for a,b in sorted(edges,key=lambda e:(-math.dist(by_id[e[0]]['xy'],by_id[e[1]]['xy']),e))]

    async def refine(self,left,right,*,target=.5,max_rounds=16):
        if target<=0 or max_rounds<1:raise ValueError('Invalid refinement limit')
        if (left['duration']<=900)==(right['duration']<=900):raise ValueError('Expected opposite observed labels')
        if left['origin']!=right['origin'] or left['origin']!=self.origin:raise ValueError('Mixed actual origins')
        a,b=left,right;initial=[left,right];history=[];successes=0;reason=None
        for _ in range(max_rounds):
            if self.scheduler.token.cancelled:reason='cancelled';break
            if any(tuple(p['xy']) in self._conflicts for p in (a,b)):reason='conflicting_observations';break
            width=math.dist(a['xy'],b['xy'])
            if width<=target:break
            mid=[(x+y)/2 for x,y in zip(a['xy'],b['xy'])];proposal=mid;improved=False
            for proposal_index in range(2):
                kind='boundary_midpoint' if proposal_index==0 else 'offset_compensation'
                before=self.scheduler.stats.requests
                record,error=await self.measure(proposal,kind)
                step=dict(kind=kind,requested_xy=list(proposal),before_width_m=width,
                          calls=self.scheduler.stats.requests-before,record=record,reason=error,accepted=False)
                history.append(step)
                if record is None:reason=error;break
                same_a=(record['duration']<=900)==(a['duration']<=900)
                other=b if same_a else a
                new_width=math.dist(record['xy'],other['xy'])
                # Actual endpoints may bend the bracket. Both old endpoints must
                # remain nearby, and the new opposite pair must strictly shrink.
                if (self.domain.covers(Point(record['xy'])) and new_width<=.75*width
                        and max(math.dist(record['xy'],p['xy']) for p in (a,b))<=width+1e-6):
                    if same_a:a=record
                    else:b=record
                    step.update(accepted=True,after_width_m=new_width);improved=True;successes+=1;reason=None;break
                reason='offset_stagnation';step['reason']=reason
                offset=[v-r for v,r in zip(record['xy'],record['request_xy'])]
                proposal=[v-d for v,d in zip(mid,offset)]
            if not improved:break
        width=math.dist(a['xy'],b['xy'])
        cancelled=reason=='cancelled' or self.scheduler.token.cancelled
        if cancelled:reason='cancelled'
        localized=width<=target and not cancelled and reason!='conflicting_observations'
        if not localized and reason is None:reason='iteration_limit'
        midpoint=[(x+y)/2 for x,y in zip(a['xy'],b['xy'])]
        valid=not cancelled and reason!='conflicting_observations'
        fraction=(900-a['duration'])/(b['duration']-a['duration'])
        estimate=dict(kind='linear_fallback',xy=midpoint,duration_seconds=(a['duration']+b['duration'])/2,
            threshold_crossing_xy=[x+fraction*(y-x) for x,y in zip(a['xy'],b['xy'])],
            source_ids=[a['id'],b['id']],usedForClassification=False) if valid else None
        return dict(status='cancelled' if cancelled else 'localized' if localized else 'unfinished',
            reason=None if localized else reason,initial=initial,left=a,right=b,width_m=width,
            iterations=successes,history=history,midpoint_xy=midpoint if valid else None,
            reachable_endpoint=a if a['duration']<=900 else b,interior_classification=None,
            evidence='boundary_uncertain',linear_estimate=estimate,
            suspected_jump=bool(localized and successes>=3 and abs(a['duration']-b['duration'])>120))
