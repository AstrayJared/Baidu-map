import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Drawer, InputNumber, Select, Space, Tag } from 'antd';
import type { Center } from '../types';
import { createApiService } from './service';
import { AnalysisController } from './controller';
import type { AnalysisResult, AnalysisState, Budget, Isochrone } from './types';
import { isAnalysisBusy } from './types';
import { ApiMap, type Layers } from './ApiMap';
import { LocationControls } from './LocationControls';
import { geometryMessage } from './geometry';
import { analysisAvailability, withFacilityRoute } from './adapter';
import { AnalysisReport } from './AnalysisReport';
import { AnalysisProgress } from './AnalysisProgress';
import './api.css';
import { FacilityPanel } from './FacilityPanel';

const reasons: Record<string, string> = { budget: '达到调用预算', deadline: '达到截止时间', resolution_limit: '达到网格分辨率或完成边界检查', maximum_range: '达到最大范围', permission: '步行接口权限异常', quota: '服务配额不足', invalid_parameter: '上游参数被拒绝', upstream_failure: '连续上游故障', geometry_error: '几何重建失败' };
const warnings: Record<string, string> = { range_unknown: '外缘存在未知样本，范围尚未核实', range_truncated: '可达边界可能被计算范围截断', unfinished_boundary: '部分边界尚未完成细化', endpoints_unverified: '部分路线端点尚未核验', geometry_error: '几何重建失败' };

function ResultSummary({ result }: { result: Isochrone }) {
  return <>
    <Alert type={result.quality === 'usable' ? 'success' : 'warning'} title={geometryMessage(result.geometry)} description={`质量：${{ usable: '可用', partial: '部分结果', insufficient: '证据不足' }[result.quality]}`} showIcon />
    <dl className="api-statistics">
      <dt>停止原因</dt><dd>{reasons[result.stopReason] || result.stopReason}</dd>
      <dt>Provider 调用</dt><dd>{result.statistics.requests} 次</dd>
      <dt>网络尝试预留</dt><dd>{result.statistics.network_requests} 次</dd>
      <dt>重试</dt><dd>{result.statistics.retries} 次</dd>
      <dt>未知面积</dt><dd>{(result.statistics.unknown_area / 1e6).toFixed(3)} 平方公里</dd>
      <dt>未完成边界格</dt><dd>{result.statistics.unfinished_boundary}</dd>
      <dt>总耗时</dt><dd>{result.statistics.total_seconds.toFixed(2)} 秒</dd>
    </dl>
    {result.warnings.map(warning => <Alert key={warning} type="warning" title={warnings[warning] || warning} />)}
  </>;
}

export default function ApiApp() {
  const [center, setCenter] = useState<Center>({ lng: 116.404, lat: 39.915 });
  const [lng, setLng] = useState<number | null>(116.404);
  const [lat, setLat] = useState<number | null>(39.915);
  const [budget, setBudget] = useState<Budget>(400);
  const [minutes,setMinutes] = useState(15);
  const [group,setGroup] = useState('all');
  const [selected,setSelected] = useState<string|null>(null);
  const [showFacilities,setShowFacilities] = useState(true);
  const [showAssessments,setShowAssessments] = useState(true);
  const [route,setRoute] = useState<{taskId:string;points:[number,number][]}|null>(null);
  const [state, setState] = useState<AnalysisState>({ phase: 'idle' });
  const [lastResult, setLastResult] = useState<AnalysisResult>();
  const [dirty, setDirty] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const [layers, setLayers] = useState<Layers>({ reachable: true, unreachable: true, unknown: true, uncertain: true, extent: false, serviceBlind: true });
  const controller = useRef<AnalysisController | null>(null);
  useEffect(() => {
    const instance = new AnalysisController(createApiService(), setState);
    controller.current = instance;
    return () => { instance.dispose(); controller.current = null; };
  }, []);
  useEffect(() => {
    if (state.phase !== 'completed' || !state.result || analysisAvailability(state.result) === 'unavailable') return;
    setLastResult(state.result);
    setDirty(false);
    setReportOpen(true);
  }, [state.phase, state.result]);
  const displayedResult = lastResult;
  const unavailable = !!state.result && analysisAvailability(state.result) === 'unavailable';
  const lastAttemptFailed = state.phase === 'error' || unavailable;
  const busy = isAnalysisBusy(state);
  const valid = lng !== null && lat !== null && Number.isFinite(lng) && Number.isFinite(lat) && lng >= -180 && lng <= 180 && lat > -85 && lat < 85;
  function choose(next: Center) {
    setDirty(true);
    void controller.current?.reset();
    setCenter(next); setLng(next.lng); setLat(next.lat);
  }
  function edit(axis: 'lng' | 'lat', value: number | null) {
    setDirty(true);
    void controller.current?.reset();
    if (axis === 'lng') setLng(value); else setLat(value);
  }
  function analyze() {
    if (!valid || busy) return;
    const next = { lng: +lng!.toFixed(6), lat: +lat!.toFixed(6) };
    setCenter(next); setLng(next.lng); setLat(next.lat);
    void controller.current?.start({ center: next, budget });
  }
  return <div className="api-app">
    <header className="api-header"><div><span className="api-brand">15</span><div><h1>15 分钟生活圈</h1><p>自适应网格 · 步行等时圈分析</p></div></div><Tag color="teal">后端算法入口</Tag></header>
    <main className="api-layout">
      <section className="api-controls" aria-label="分析条件">
        <Card title="选择分析中心"><p className="api-muted">在地图上选点、获取当前位置或搜索地点，也可输入百度坐标。默认坐标仅用于选点起始位置，尚未进行社区实验验证。</p>
          <LocationControls center={center} onPick={choose} />
          <label className="api-label">经度<InputNumber aria-label="经度" value={lng} onChange={value => edit('lng', value)} precision={6} /></label>
          <label className="api-label">纬度<InputNumber aria-label="纬度" value={lat} onChange={value => edit('lat', value)} precision={6} /></label>
          <label className="api-label">等时圈采样预算<Select aria-label="调用预算" value={budget} onChange={value => { setDirty(true); void controller.current?.reset(); setBudget(value); }} options={[200, 400, 800].map(value => ({ value, label: `${value} 次` }))} /></label>
          <p className="api-muted">步行阈值 900 秒 · 坐标系 BD09LL</p>
          {!valid && <Alert type="error" title="请输入有效坐标：经度 −180～180，纬度大于 −85 且小于 85" />}
          <Space wrap><Button aria-label="开始分析" type="primary" onClick={analyze} disabled={!valid || busy} loading={busy}>开始分析</Button>
            {(busy || (state.phase === 'error' && state.task)) && <Button onClick={() => void controller.current?.cancel()} disabled={state.phase === 'cancelling'}>取消任务</Button>}</Space>
        </Card>
        <Card title="地图图层"><div className="api-layer-list">{([['reachable', '可达区域'], ['unreachable', '已知不可达区域'], ['unknown', '未核验区域（灰色）'], ['serviceBlind', '设施服务盲区（灰色）'], ['uncertain', '不确定区域'], ['extent', '计算范围']] as const).map(([key, label]) => <Checkbox key={key} checked={layers[key]} onChange={event => setLayers({ ...layers, [key]: event.target.checked })}><i className={`api-swatch ${key}`} />{label}</Checkbox>)}</div></Card>
        <label className="api-label">步行时间层<Select aria-label="步行时间层" value={minutes} onChange={setMinutes} options={[5,10,15].map(value=>({value,label:`${value} 分钟`}))}/></label>
        <Checkbox checked={showFacilities} onChange={e=>setShowFacilities(e.target.checked)}>设施标记</Checkbox>
        <Checkbox checked={showAssessments} onChange={e=>setShowAssessments(e.target.checked)}>点位三态</Checkbox>
        {!displayedResult?.facilityAnalysis && <Alert type="info" title="设施统计尚未接入" description="真实步行分析结束后将继续检索设施并核对抽样点；合成模式只验证等时圈。" />}
      </section>
      <section className="api-map-section">
        {lastResult && dirty && <Alert type="warning" title="条件已修改，需重新分析。旧结果仍属于原中心点和预算。" showIcon />}
        <ApiMap
          center={center}
          result={displayedResult?.isochrone}
          resultCenter={displayedResult?.center}
          layers={layers}
          onPick={choose}
          minutes={minutes}
          facilities={showFacilities ? (displayedResult?.data.facilities ?? []).filter(f => group === 'all' || f.major_category === group) : []}
          assessments={showAssessments ? (displayedResult?.facilityAnalysis?.assessments ?? []).map(p => ({ ...p, categories: p.categories.filter(c => group === 'all' || c.category === group) })) : []}
          blindRegions={displayedResult?.facilityAnalysis?.serviceBlindRegions ?? {}}
          selected={selected}
          onFacility={id => { setSelected(id); setRoute(null); }}
          route={route && route.taskId === displayedResult?.taskId ? route.points : []}
        />
      </section>
      <section className="api-results" aria-label="分析结果"><Card title="分析结果">
        {state.phase === 'idle' && <p className="api-muted">选择中心后开始分析。结果将展示可达区域及证据质量。</p>}
        <AnalysisProgress state={state} />
        {state.phase === 'cancelled' && <Alert title={state.task ? '任务已取消' : '已取消本次分析等待'} description={!state.task ? '不会采用本次结果；仍在创建的任务将在收到响应后取消。' : undefined} type="info" />}
        {state.error && <Alert type="error" title={state.error} action={<Button aria-label="重试" size="small" onClick={() => void controller.current?.retry()}>重试</Button>} />}
        {lastResult && lastAttemptFailed && <Alert type="warning" title="本次分析未获得可用结果，仍可查看上一次报告。" />}
        {unavailable && <Alert type="warning" title="本次步行证据不足，未生成新的体检报告。" />}
        {unavailable && !lastResult && state.result && <ResultSummary result={state.result.isochrone} />}
        {displayedResult && <><ResultSummary result={displayedResult.isochrone} /><p className="api-muted">结果来源：{displayedResult.dataSource === 'synthetic' ? '合成时间场' : '百度步行数据'}<br />结果中心：{displayedResult.center.lng.toFixed(6)}, {displayedResult.center.lat.toFixed(6)}<br />{new Date(displayedResult.generatedAt * 1000).toLocaleString('zh-CN')}</p><details><summary>查看机器可读结果</summary><pre>{JSON.stringify(displayedResult, null, 2)}</pre></details></>}
        <Button block disabled={!lastResult} onClick={() => setReportOpen(true)}>查看分析报告</Button>
       </Card>{displayedResult?.facilityAnalysis && <FacilityPanel key={displayedResult.taskId} result={displayedResult} group={group} onGroup={setGroup} selected={selected} onSelect={setSelected} onRoute={(points, evidence)=>{
          const taskId = displayedResult.taskId;
          setRoute({ taskId, points });
          if (evidence?.poiEvidence) setLastResult(previous => previous?.taskId === taskId ? withFacilityRoute(previous, evidence) : previous);
        }}/>}</section>
    </main>
    <footer className="api-footer">质量提示随采样证据展示；未知区域不代表不可达。设施判断仅代表有证据的采样点，不推断盲区面积。</footer>
    <Drawer title="生活圈分析报告" open={reportOpen} onClose={() => setReportOpen(false)} size={680}>
      {lastResult && <AnalysisReport result={lastResult} stale={dirty} lastAttemptFailed={lastAttemptFailed} />}
    </Drawer>
  </div>;
}
