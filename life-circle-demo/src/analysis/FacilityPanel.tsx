import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Select, Tag } from 'antd';
import type { AnalysisResult } from './types';
import type { RouteEvidence } from '../api-contract';
import './facilities.css';
import { withFacilityRoute } from './adapter';
import { poiStatusLabel, presentRoute, requestFacilityRoute } from './routes';

export const groupNames: Record<string,string> = { shopping:'购物', medical:'医疗服务', education:'教育' };
const statusNames: Record<string,string> = {covered:'有设施',blind:'查询范围内盲区',unknown:'无法判断'};

export function FacilityPanel({result, group, onGroup, selected, onSelect, onRoute}: {result:AnalysisResult; group:string; onGroup:(g:string)=>void; selected:string|null; onSelect:(id:string)=>void; onRoute:(route:[number,number][], evidence?:RouteEvidence)=>void}) {
  const [route, setRoute] = useState<RouteEvidence|null>(null);
  const [error,setError] = useState('');
  const [loading,setLoading] = useState(false);
  const revision=useRef(0);
  const pending=useRef<AbortController | null>(null);
  const resultRef=useRef(result);
  resultRef.current=result;
  useEffect(()=>{revision.current++;pending.current?.abort();setRoute(null);setError('');setLoading(false);return ()=>{revision.current++;pending.current?.abort();};},[selected,result]);
  const shownRoute = route ?? (selected ? result.facilityAnalysis?.routes[selected] : null);
  const routeView = shownRoute ? presentRoute(shownRoute) : null;
  const analysis = result.facilityAnalysis;
  if (!analysis) return null;
  const facilities = (result.data.facilities || []).filter(f=>group==='all'||f.major_category===group);
  const current = (result.data.facilities||[]).find(f=>f.id===selected);
  async function showRoute(id:string) {
    const currentRevision=++revision.current;
    pending.current?.abort();
    const controller=new AbortController();
    pending.current=controller;
    const isCurrent=()=>revision.current===currentRevision && resultRef.current===result && !controller.signal.aborted;
    setLoading(true);setError('');setRoute(null);onRoute([]);
    try {
      const value = await requestFacilityRoute(result.taskId, id, controller.signal);
      if (isCurrent()) {
        withFacilityRoute(result, value); // Validate task center and destination before drawing.
        setRoute(value);onRoute(presentRoute(value).path, value);
      }
    } catch(e) {if(isCurrent())setError(e instanceof Error?e.message:'路线获取失败');}
    finally {if(isCurrent())setLoading(false);}
  }
  return <Card title="设施与基础报告" className="facility-panel">
    <Alert type="info" title="同一次分析的设施与点位核对" description={result.data.report}/>
    <p>设施检索及距离查询：{analysis.network_requests} 次 · {analysis.elapsed_seconds.toFixed(1)} 秒</p>
    <p>已评估 {analysis.assessed_points} / {analysis.candidate_points} 个实测可达点，另有 {analysis.unassessed_points} 点未判定。</p>
    <Select aria-label="设施类别" value={group} onChange={onGroup} options={[{value:'all',label:'全部设施'},...Object.entries(groupNames).map(([value,label])=>({value,label}))]}/>
    <div className="facility-counts">{Object.entries(groupNames).map(([key,label])=>{const items=(result.data.facilities||[]).filter(f=>f.major_category===key);const count=items.filter(f=>f.in_circle===true).length;const unknown=items.filter(f=>f.in_circle===null).length;return <div key={key}><strong>{label}</strong><span>检索 {items.length} · 估算圈内记录 {count}{unknown > 0 && ` · 圈内关系未知 ${unknown}`}</span><progress value={count} max={Math.max(1,items.length)} aria-label={`${label}圈内记录`}/></div>;})}</div>
    <p>列表 {facilities.length} 处，地图最多显示100个设施标记。圈内计数不等于1公里步行覆盖。</p>
    <div className="facility-list">{facilities.map(f=><button key={f.id} className={selected===f.id?'selected':''} onClick={()=>{onSelect(f.id);setRoute(null);setError('');onRoute([]);}}><strong>{f.name}</strong><span>{groupNames[f.major_category]} · {f.in_circle===null?'圈内关系未知':f.in_circle?'估算圈内':'估算圈外'}</span><span>{poiStatusLabel(f.poiEvidence)}</span></button>)}</div>
    {current && <div className="facility-detail"><h3>{current.name}</h3><p>{current.location.lng.toFixed(6)}, {current.location.lat.toFixed(6)}</p><Button loading={loading} disabled={loading} onClick={()=>void showRoute(current.id)}>{loading ? '路线查询中…' : '查看中心到设施的步行路线'}</Button>{routeView && <p>{routeView.distance??'未知'} 米 · {routeView.duration??'未知'} 秒 · {routeView.message}</p>}{shownRoute?.poiEvidence && <div data-testid="route-poi-evidence"><p>{poiStatusLabel(shownRoute.poiEvidence)}</p><p>实际端点观测耗时：{shownRoute.poiEvidence.observedDuration ?? '未知'} 秒（不能替代请求设施的有效耗时）。端点{shownRoute.poiEvidence.endpointVerified ? '已解析' : '未解析'}。</p><p>起点偏移：{shownRoute.poiEvidence.originOffsetM ?? '未知'} 米；终点偏移：{shownRoute.poiEvidence.destinationOffsetM ?? '未知'} 米。</p>{shownRoute.poiEvidence.reason && <p>证据说明：{shownRoute.poiEvidence.reason}</p>}</div>}{error&&<Alert type="warning" title={error}/>}</div>}
    <h3>实测点位的1公里三态</h3><div className="assessment-list">{analysis.assessments.map((p,i)=><div key={i}><strong>点 {i+1} · {p.duration_s.toFixed(0)} 秒</strong><span>{p.location.lng.toFixed(6)}, {p.location.lat.toFixed(6)}</span>{p.categories.filter(c=>group==='all'||c.category===group).map(c=><Tag key={c.category} color={c.status==='covered'?'green':c.status==='blind'?'orange':'default'}>{groupNames[c.category]}：{statusNames[c.status]}{c.distance_m!==null?`（${c.distance_m}米）`:''}</Tag>)}</div>)}</div>
    {analysis.queries.map(q=><p key={q.category} className="api-muted">{q.query}：{q.returned} 条原始记录，{q.pages} 页 · {{complete:'本次查询完成',partial:'部分记录待确认',failed:'查询失败',truncated:'分页已截断'}[q.status]}</p>)}
    {analysis.warnings.map(w=><Alert type="warning" title={w} key={w}/>)}
  </Card>;
}
