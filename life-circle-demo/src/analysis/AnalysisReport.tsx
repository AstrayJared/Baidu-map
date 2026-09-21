import { Alert } from 'antd';
import { reportWarnings } from '../report';
import { analysisReportView } from './adapter';
import type { AnalysisResult } from './types';
import styles from '../styles.module.css';

/** Geographic report boundary; the demo ReportPage and its preset claims remain isolated. */
export function AnalysisReport({ result, stale, lastAttemptFailed }: {
  result: AnalysisResult; stale: boolean; lastAttemptFailed: boolean;
}) {
  const view = analysisReportView(result);
  return <article className={styles.report} data-testid="analysis-report">
    <div className={styles.eyebrow}>COMMUNITY CHECKUP / ANALYSIS</div>
    <h1>15 分钟生活圈分析报告</h1>
    {reportWarnings({ stale, lastAttemptFailed }).map(w => <Alert key={w.kind} type="warning" title={w.message} showIcon />)}
    <Alert type="warning" title={result.facilityAnalysis ? "部分体检结果：设施为有限检索，盲区面积尚未验证" : "部分体检结果：设施与服务盲区尚未接入"} showIcon />
    <dl>
      <div><dt>分析中心点</dt><dd>{view.center.lng.toFixed(6)}, {view.center.lat.toFixed(6)}</dd></div>
      <div><dt>分析时间</dt><dd>{new Date(view.generatedAt).toLocaleString('zh-CN', { hour12: false })}</dd></div>
      <div><dt>数据来源</dt><dd>{view.dataSource}</dd></div>
      <div><dt>降级采样预算</dt><dd>{view.budget} 条路线</dd></div>
      <div><dt>坐标系</dt><dd>BD09LL（经度、纬度）</dd></div>
    </dl>
    <h2>01 / 步行等时圈</h2>
    <p>{view.geometrySummary}；证据质量：{view.qualityLabel}。步行阈值为 900 秒。</p>
    <p>成圈预算已使用 {view.statistics.requests} 单位；矩阵路线对 {view.statistics.matrix_route_pairs ?? 0}，详细路线 {view.statistics.detailed_route_requests ?? 0}，实际发送／响应／终止 {view.statistics.sends ?? view.statistics.network_requests}／{view.statistics.responses ?? view.statistics.network_requests}／{view.statistics.terminations ?? 0}，重试 {view.statistics.retries} 次。</p>
    <p>未知面积 {(view.statistics.unknown_area / 1e6).toFixed(3)} 平方公里，未完成边界格 {view.statistics.unfinished_boundary} 个。</p>
    <h2>02 / 设施与服务盲区</h2>
    {result.facilityAnalysis && <p data-testid="strict-poi-counts">严格步行核验：15分钟内可达 {view.poiCounts.reachable}；返回路线超过15分钟 {view.poiCounts.unreachable}；待核验（未知）{view.poiCounts.pending}；旧版未提供证据 {view.poiCounts.legacy}。未核验不等于不可达。</p>}
    <table className={styles.reportTable} data-testid="analysis-facility-stats">
      <thead><tr><th>设施类别</th><th>圈内数量</th><th>数据状态</th></tr></thead>
      <tbody>{view.facilityStats.map(s => <tr key={s.category}><td>{s.label}</td><td>{s.count ?? '无法确定'}</td><td>{s.state}</td></tr>)}</tbody>
    </table>
    <p>设施盲区数量：{view.blindZoneCount ?? '无法确定'}。{result.facilityAnalysis ? result.data.report : '后端未执行设施检索和 1 公里服务评估，不能判断设施缺失或服务充分。'}</p>
    <h2>03 / 结果适用边界</h2>
    <p>未知区域表示缺少步行证据，不代表不可达或设施盲区；不确定区域表示边界尚待核实。可达区域为空与无法确定可达区域含义不同。</p>
    {result.dataSource === 'synthetic' && <p>本次使用合成 Provider，仅供流程和算法验收，不代表真实社区。</p>}
    <p>结果对应上述中心点；采样预算不代表全社区人口覆盖率、设施容量、质量或使用资格。</p>
    {view.warnings.length > 0 && <details><summary>算法质量标记</summary><ul>{view.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></details>}
  </article>;
}
