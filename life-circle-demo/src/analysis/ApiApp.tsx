import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Drawer, InputNumber, Select, Space, Tag } from 'antd';
import type { Center } from '../types';
import type { AnalysisResult, Budget, Isochrone } from './types';
import { isAnalysisBusy } from './types';
import { ApiMap, type Layers } from './ApiMap';
import { LocationControls } from './LocationControls';
import { geometryMessage } from './geometry';
import { analysisAvailability, withFacilityRoute } from './adapter';
import { heatNoticeFor, heatPointsOf, undeterminedInCircleOf } from './heat';
import { AnalysisReport } from './AnalysisReport';
import { AnalysisProgress } from './AnalysisProgress';
import './api.css';
import { FacilityPanel } from './FacilityPanel';
import { getBaiduSession, saveBaiduSession } from '../algorithmSessions';
import { baiduTasks, useLegacyState } from '../legacyTasks';
import { displayGroupForMajor } from '../taxonomy';

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

/** 这几个阶段是在这次挂载里亲眼看着跑的：跑完才自动打开报告（刷新后取回旧结果不算）。 */
const LIVE_PHASES = new Set(['submitting', 'running']);
const PHASE_LABELS: Record<string, string> = { submitting: '正在提交', restoring: '正在核对上次的任务',
  running: '运行中', fetching: '正在取结果', cancelling: '正在取消', completed: '已完成', cancelled: '已取消', error: '出错' };

export default function ApiApp() {
  const session = getBaiduSession();
  // 任务在模块里的控制器上：这个组件卸载（换页面、换算法）只是退订，任务照跑。
  const tasks = baiduTasks();
  const controller = tasks.controller;
  const state = useLegacyState(tasks);
  const [center, setCenter] = useState<Center>(session.center);
  const [lng, setLng] = useState<number | null>(session.lng);
  const [lat, setLat] = useState<number | null>(session.lat);
  const [budget, setBudget] = useState<Budget>(session.budget);
  const [minutes,setMinutes] = useState(15);
  const [group,setGroup] = useState(displayGroupForMajor(session.group) ?? session.group);
  const [selected,setSelected] = useState<string|null>(session.selected);
  const [showFacilities,setShowFacilities] = useState(session.showFacilities);
  const [showAssessments,setShowAssessments] = useState(session.showAssessments);
  const [route,setRoute] = useState(session.route);
  const [lastResult, setLastResult] = useState<AnalysisResult | undefined>(() => session.lastResult
    ?? (state.phase === 'completed' && state.result && analysisAvailability(state.result) !== 'unavailable'
      ? state.result : undefined));
  const [reportOpen, setReportOpen] = useState(session.reportOpen);
  const [layers, setLayers] = useState<Layers>(session.layers);
  const sawLive = useRef(false);
  useEffect(() => {
    if (LIVE_PHASES.has(state.phase)) sawLive.current = true;
    if (state.phase !== 'completed' || !state.result || analysisAvailability(state.result) === 'unavailable') return;
    setLastResult(state.result);
    if (sawLive.current) { sawLive.current = false; setReportOpen(true); }
  }, [state.phase, state.result]);
  useEffect(() => {
    saveBaiduSession({ center, lng, lat, budget, group, selected, showFacilities, showAssessments,
      route, lastResult, reportOpen, layers });
  }, [center, lng, lat, budget, group, selected, showFacilities, showAssessments, route, lastResult, reportOpen, layers]);
  const displayedResult = lastResult;
  // "条件已修改"按内容算：左栏的中心或预算与地图上这份结果的不同。任务在别处跑完再回来也不会误判。
  const dirty = !!lastResult && (lng === null || lat === null || lastResult.center.lng !== +lng.toFixed(6)
    || lastResult.center.lat !== +lat.toFixed(6) || lastResult.isochrone.config.budget !== budget);
  // 传给地图的这几组数据必须按内容保持身份稳定：每次渲染都新建数组会让图层误以为
  // 数据变了，从而在整个地图上重建覆盖物（标记顺序、选中态、用户视角都会被重置）。
  const mapFacilities = useMemo(() =>
    !showFacilities ? [] : (displayedResult?.data.facilities ?? []).filter(f => group === 'all'
      || f.displayGroup === group || displayGroupForMajor(f.major_category) === group || f.major_category === group),
  [showFacilities, displayedResult, group]);
  const mapHeatPoints = useMemo(() => heatPointsOf(displayedResult?.data.facilities, group), [displayedResult, group]);
  const mapHeatUndetermined = useMemo(() => undeterminedInCircleOf(displayedResult?.data.facilities, group), [displayedResult, group]);
  const mapAssessments = useMemo(() => !showAssessments ? [] : (displayedResult?.facilityAnalysis?.assessments ?? [])
    .map(p => ({ ...p, categories: p.categories.filter(c => group === 'all'
      || displayGroupForMajor(c.category) === group || c.category === group) })),
  [showAssessments, displayedResult, group]);
  const mapRoute = useMemo(() => route && route.taskId === displayedResult?.taskId ? route.points : [],
  [route, displayedResult]);
  const emptyBlindRegions = useMemo(() => ({}), []);
  const unavailable = !!state.result && analysisAvailability(state.result) === 'unavailable';
  const lastAttemptFailed = state.phase === 'error' || unavailable;
  const busy = isAnalysisBusy(state);
  const live = controller.hasLiveTask;
  const valid = lng !== null && lat !== null && Number.isFinite(lng) && Number.isFinite(lat) && lng >= -180 && lng <= 180 && lat > -85 && lat < 85;
  const taskCenter = state.input?.center;
  const movedFromTask = !!taskCenter && valid && (taskCenter.lng !== +lng!.toFixed(6) || taskCenter.lat !== +lat!.toFixed(6));
  // 选点、改坐标、改预算只改左栏的草稿，不碰正在跑的任务。
  function choose(next: Center) {
    setCenter(next); setLng(next.lng); setLat(next.lat);
  }
  function edit(axis: 'lng' | 'lat', value: number | null) {
    if (axis === 'lng') setLng(value); else setLat(value);
  }
  function analyze() {
    if (!valid || busy || live) return;
    const next = { lng: +lng!.toFixed(6), lat: +lat!.toFixed(6) };
    setCenter(next); setLng(next.lng); setLat(next.lat);
    void controller.start({ center: next, budget });
  }
  return <div className="api-app">
    <header className="api-header"><div><span className="api-brand">15</span><div><h1>15 分钟生活圈</h1><p>百度边界搜索 · 实际端点证据与局部细化</p></div></div><Tag color="teal">E8.2</Tag></header>
    <main className="api-layout">
      <section className="api-controls" aria-label="分析条件">
        <Card title="选择分析中心"><p className="api-muted">在地图上选点、获取当前位置或搜索地点，也可输入百度坐标。默认坐标仅用于选点起始位置，尚未进行社区实验验证。</p>
          <LocationControls center={center} onPick={choose} />
          <label className="api-label">经度<InputNumber aria-label="经度" value={lng} onChange={value => edit('lng', value)} precision={6} /></label>
          <label className="api-label">纬度<InputNumber aria-label="纬度" value={lat} onChange={value => edit('lat', value)} precision={6} /></label>
          <label className="api-label">等时圈采样预算<Select aria-label="调用预算" value={budget} onChange={setBudget} options={[200, 400, 800].map(value => ({ value, label: `${value} 次` }))} /></label>
          <p className="api-muted">步行阈值 900 秒 · 坐标系 BD09LL</p>
          {!valid && <Alert type="error" title="请输入有效坐标：经度 −180～180，纬度大于 −85 且小于 85" />}
          <Space wrap><Button aria-label="开始分析" type="primary" onClick={analyze} disabled={!valid || busy || live} loading={busy}>开始分析</Button>
            {live && <Button onClick={() => void controller.cancel()} disabled={state.phase === 'cancelling'}>取消任务</Button>}</Space>
          {live && <p className="api-muted" data-testid="legacy-live-note">当前任务尚未结束：切换页面、切换算法或刷新都不会取消它。
            要按新条件分析，请等它完成，或先点"取消任务"。</p>}
        </Card>
        <Card title="地图图层"><div className="api-layer-list">{([['reachable', '可达区域'], ['unreachable', '已知不可达区域'], ['unknown', '未核验区域（灰色）'], ['serviceBlind', '设施服务盲区（灰色）'], ['uncertain', '不确定区域'], ['extent', '计算范围'], ['heatmap', '设施密度热力（真实 POI）']] as const).map(([key, label]) => <Checkbox key={key} checked={layers[key]} onChange={event => setLayers({ ...layers, [key]: event.target.checked })}><i className={`api-swatch ${key}`} />{label}</Checkbox>)}</div></Card>
        <label className="api-label">步行时间层<Select aria-label="步行时间层" value={minutes} onChange={setMinutes} options={[15].map(value=>({value,label:`${value} 分钟`}))}/></label>
        <Checkbox checked={showFacilities} onChange={e=>setShowFacilities(e.target.checked)}>设施标记</Checkbox>
        <Checkbox checked={showAssessments} onChange={e=>setShowAssessments(e.target.checked)}>点位三态</Checkbox>
        {!displayedResult?.facilityAnalysis && <Alert type="info" title="设施统计尚未接入" description="真实步行分析结束后将继续检索设施并核对抽样点；合成模式只验证等时圈。" />}
      </section>
      <section className="api-map-section" data-testid="legacy-map-section" data-task-id={displayedResult?.taskId ?? ''}>
        {lastResult && dirty && <Alert type="warning" title="条件已修改，需重新分析。旧结果仍属于原中心点和预算。" showIcon />}
        {live && movedFromTask && <Alert type="info" showIcon
          title="进行中的任务仍按它提交时的中心计算；新选点要等它结束后再分析。" />}
        <ApiMap
          center={center}
          result={displayedResult?.isochrone}
          resultCenter={displayedResult?.center}
          layers={layers}
          onPick={choose}
          minutes={minutes}
          facilities={mapFacilities}
          heatPoints={mapHeatPoints}
          heatNotice={heatNoticeFor(displayedResult?.facilityAnalysis)}
          heatUndetermined={mapHeatUndetermined}
          assessments={mapAssessments}
          blindRegions={displayedResult?.facilityAnalysis?.serviceBlindRegions ?? emptyBlindRegions}
          selected={selected}
          onFacility={id => { setSelected(id); setRoute(null); }}
          route={mapRoute}
        />
      </section>
      <section className="api-results" aria-label="分析结果"><Card title="分析结果">
        {state.phase === 'idle' && <p className="api-muted">选择中心后开始分析。结果将展示可达区域及证据质量。</p>}
        {state.phase !== 'idle' && <p className="api-muted" data-testid="legacy-phase" data-phase={state.phase}>
          状态：{PHASE_LABELS[state.phase] ?? state.phase}
          {state.input && `（中心 ${state.input.center.lng.toFixed(6)}, ${state.input.center.lat.toFixed(6)} · 预算 ${state.input.budget} 次）`}
          {state.task && <><br />任务 <span data-testid="legacy-task-id">{state.task.taskId}</span></>}</p>}
        {state.connection === 'lost' && <Alert type="warning" showIcon data-testid="legacy-connection"
          title="与分析服务的连接中断，正在自动重连"
          description="任务仍在服务端继续，不会因为断网被取消或重复提交；连上后自动接着显示进度与结果。" />}
        {state.phase === 'restoring' && <Alert type="info" showIcon title="正在向后端核对上次的分析任务" />}
        {state.notice && <Alert type="info" showIcon data-testid="legacy-notice" title={state.notice} />}
        <AnalysisProgress state={state} />
        {state.phase === 'cancelled' && <Alert title="任务已取消" type="info"
          description="取消只停住后续请求；已经发出的调用仍计入步行服务额度。" />}
        {state.error && <Alert type="error" title={state.error} data-testid="legacy-error" action={<Button aria-label="重试" size="small"
          onClick={() => void controller.retry()}>{state.recovery === 'unconfirmed' ? '重新提交'
            : state.recovery === 'expired' || state.task?.status === 'failed' ? '重新分析' : '重试'}</Button>} />}
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
