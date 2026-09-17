import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, InputNumber, Select, Space, Tag } from 'antd';
import type { Center } from '../types';
import type { HybridResultResponse, TaskStatusResponse } from '../api-contract';
import { ApiMap, type Layers } from '../analysis/ApiMap';
import { LocationControls } from '../analysis/LocationControls';
import { createHybridClient, HybridApiError } from './client';
import '../analysis/api.css';

const api = createHybridClient();
const initial = { lng: 121.513925, lat: 31.313079 };
const terminal = (task: TaskStatusResponse) => ['completed', 'cancelled', 'failed'].includes(task.status);
const quality = { usable: '可用', partial: '部分结果', insufficient: '证据不足' };
const stopReasons: Record<string, string> = { budget: '达到验证预算', deadline: '达到时间上限',
  refinement_complete: '完成本轮细化', no_candidates: '没有可继续核验的候选点',
  synthetic_contract_fixture: '离线演示数据', cancelled: '已取消', upstream_failure: '步行服务暂不可用' };
const errorMessages: Record<string, string> = { baidu_walking_not_configured: '后端尚未配置百度步行服务',
  hybrid_busy: '服务正在处理其他分析，请稍后重试', hybrid_execution_failed: '分析执行失败，请检查数据配置',
  hybrid_invalid_response: '服务返回的结果格式异常', hybrid_task_mismatch: '结果与当前任务不匹配',
  hybrid_invalid_request: '分析参数无效，请检查坐标和预算' };

export default function HybridApp() {
  const [center, setCenter] = useState<Center>(initial);
  const [lng, setLng] = useState<number | null>(initial.lng);
  const [lat, setLat] = useState<number | null>(initial.lat);
  const [budget, setBudget] = useState(400);
  const [task, setTask] = useState<TaskStatusResponse>();
  const [result, setResult] = useState<HybridResultResponse>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [dirty, setDirty] = useState(false);
  const [layers, setLayers] = useState<Layers>({ reachable: true, unreachable: false, unknown: false, uncertain: false, extent: false, serviceBlind: false });
  const active = useRef<{ requestId: string; taskId?: string; cancelled: boolean; controller: AbortController } | null>(null);
  useEffect(() => () => {
    const run = active.current;
    active.current = null;
    if (run) {
      run.cancelled = true;
      run.controller.abort();
      void api.cancelByRequest(run.requestId).catch(() => {});
    }
  }, []);
  const valid = lng !== null && lat !== null && Number.isFinite(lng) && Number.isFinite(lat)
    && lng >= -180 && lng <= 180 && lat > -85 && lat < 85;
  function pick(next: Center) {
    if (active.current) return;
    setCenter(next); setLng(next.lng); setLat(next.lat); setDirty(true);
  }
  async function start() {
    if (!valid || active.current) return;
    const origin = { lng: +lng!.toFixed(6), lat: +lat!.toFixed(6) };
    setCenter(origin); setError(''); setTask(undefined); setBusy(true);
    const run = { requestId: crypto.randomUUID(), taskId: undefined as string | undefined,
      cancelled: false, controller: new AbortController() };
    active.current = run;
    let finished = false;
    try {
      let current: TaskStatusResponse;
      try {
        current = await api.create({ origin, coordinate_system: 'bd09ll',
          config: { max_baidu_requests: budget }, client_request_id: run.requestId });
      } catch (createError) {
        // A lost create response must not create a duplicate billed task.
        try { current = await api.byRequest(run.requestId); } catch { throw createError; }
      }
      run.taskId = current.taskId;
      if (active.current !== run) { await api.cancel(current.taskId); return; }
      if (run.cancelled) current = await api.cancel(current.taskId);
      while (active.current === run) {
        setTask(current);
        if (terminal(current)) { finished = true; break; }
        await new Promise(resolve => setTimeout(resolve, 1000));
        if (active.current !== run) return;
        current = await api.status(current.taskId, run.controller.signal);
      }
      if (current.status === 'completed' && !run.cancelled) {
        const value = await api.result(current.taskId, run.controller.signal);
        if (active.current === run) { setResult(value); setDirty(false); }
      } else if (current.status === 'failed') {
        throw new Error(current.error || '分析失败');
      }
    } catch (reason) {
      if (active.current === run) {
        setError(`分析未完成：${reason instanceof Error ? errorMessages[reason.message] || '请检查服务连接或重试取消当前任务' : '请检查服务连接'}`);
        // Preserve uncertain task handles; known rejections and terminal failures can retry.
        if (!finished && !(reason instanceof HybridApiError && !run.taskId && [409, 422, 503].includes(reason.status))) return;
      }
    } finally {
      if (active.current === run) setBusy(false);
    }
    if (active.current === run) { active.current = null; setBusy(false); }
  }
  async function cancel() {
    const run = active.current;
    if (!run) return;
    run.cancelled = true;
    try {
      const value = run.taskId ? await api.cancel(run.taskId) : await api.cancelByRequest(run.requestId);
      setTask(value);
      if (terminal(value)) { run.controller.abort(); active.current = null; setBusy(false); }
      else if (!busy) { setError('已请求取消。可再次点击取消以确认任务终态。'); }
    } catch { setError('取消尚未确认，请重试取消。'); }
  }
  const core = result?.isochrone;
  return <div className="api-app">
    <header className="api-header"><div><span className="api-brand">15</span><div><h1>15 分钟生活圈</h1><p>OSM 路网＋百度步行验证</p></div></div><Tag color="teal">Hybrid v1.5</Tag></header>
    <main className="api-layout">
      <section className="api-controls" aria-label="分析条件">
        <Card title="选择分析中心">
          <p className="api-muted">默认使用项目固定测试点。请在已配置的 OSM 数据覆盖范围内选点。</p>
          <LocationControls center={center} onPick={pick} />
          <label className="api-label">经度<InputNumber aria-label="经度" disabled={busy || !!active.current} value={lng} onChange={v => { setLng(v); setDirty(true); }} /></label>
          <label className="api-label">纬度<InputNumber aria-label="纬度" disabled={busy || !!active.current} value={lat} onChange={v => { setLat(v); setDirty(true); }} /></label>
          <label className="api-label">百度验证预算<Select aria-label="百度验证预算" disabled={busy || !!active.current} value={budget} onChange={v => { setBudget(v); setDirty(true); }} options={[200, 400].map(value => ({ value, label: `${value} 次` }))} /></label>
          <p className="api-muted">步行阈值 900 秒 · 坐标系 BD09LL</p>
          {!valid && <Alert type="error" title="请输入有效经纬度" />}
          <Space><Button type="primary" aria-label="开始分析" disabled={!valid || busy || !!active.current} loading={busy} onClick={() => void start()}>开始分析</Button>
            {(busy || active.current) && <Button onClick={() => void cancel()}>取消任务</Button>}</Space>
        </Card>
        <Card title="地图图层">{([['reachable', '生活圈估计范围'], ['unknown', '未知区域'], ['extent', '计算范围']] as const).map(([key, label]) => <Checkbox key={key} checked={layers[key]} onChange={e => setLayers({ ...layers, [key]: e.target.checked })}>{label}</Checkbox>)}</Card>
        <Alert type="info" title="设施统计尚未接入" description="当前仅展示生活圈及步行验证证据，不将空设施列表解释为缺少服务。" />
      </section>
      <section className="api-map-section">
        {dirty && result && <Alert type="warning" title="条件已修改，地图仍显示上次分析结果" />}
        <ApiMap center={center} onPick={pick} layers={layers} resultCenter={result?.center}
          result={core ? { geometry: core.geometry, unknownRegion: core.unknown_region,
            uncertainRegion: null, computationExtent: core.computation_extent } : undefined} />
      </section>
      <section className="api-results" aria-label="分析结果"><Card title="分析结果">
        {error && <Alert type="error" title={error} />}
        {task && <p role="status">任务：{({ running: '运行中', cancelling: '取消中', cancelled: '已取消', completed: '已完成', failed: '失败' })[task.status]} · 调用 {task.requests}/{task.budget} · {task.elapsedSeconds.toFixed(1)} 秒</p>}
        {!task && <p>选择中心后开始分析。</p>}
        {core && <>
          <Alert type="warning" title={`结果质量：${quality[core.quality]}`} description="任务完成不等于独立精度验收通过；推断填充与已核验证据需区别解读。" />
          {core.readiness.mode === 'degraded' && <Alert type="warning" title="OSM 或辅助数据不完整，当前为降级结果" description="路网、覆盖边界或辅助图层未全部就绪，请结合证据范围使用结果。" />}
          {core.extent_truncated && <Alert type="warning" title="生活圈可能超出计算范围" />}
          <dl className="api-statistics"><dt>有效百度样本</dt><dd>{core.valid_baidu_samples}</dd><dt>无效样本</dt><dd>{core.invalid_baidu_samples}</dd><dt>未知样本</dt><dd>{core.unknown_samples}</dd><dt>停止原因</dt><dd>{stopReasons[core.stop_reason] || '本轮分析已结束，详见完整结果'}</dd><dt>耗时</dt><dd>{core.timing_seconds.task_total.toFixed(1)} 秒</dd><dt>结果中心</dt><dd>{result!.center.lng}, {result!.center.lat}</dd></dl>
          {core.warnings.length > 0 && <Alert type="warning" title="结果包含证据或覆盖范围限制" description="完整结果保留全部质量提示和证据图层，未知区域不应当作已确认不可达。" />}
          <details><summary>查看完整分析报告与证据</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>
        </>}
      </Card></section>
    </main>
    <footer className="api-footer">OSM 提供路网参考，百度路线提供步行核验；未知区域不代表不可达。</footer>
  </div>;
}
