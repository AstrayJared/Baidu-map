import type { CheckupCompletion } from './contract';

export function CompletionSummary({ value }: { value?: CheckupCompletion | null }) {
  if (!value) return null;
  const finished = Object.values(value.queryCompleteByMajor).filter(Boolean).length;
  const label = value.evaluationStatus === 'complete' ? '完整评估'
    : finished === value.totalCategories ? '检索已完成，评估仍受限'
    : value.evaluationStatus === 'limited' ? '评估受数据限制' : '阶段性报告';
  return <div className="checkup-completion" data-testid="checkup-completion" role="status">
    <strong>{label}</strong>
    <p>检索完成 {finished}/{value.totalCategories} 类 · 有有效覆盖判定 {value.evaluatedCategories}/{value.totalCategories} 类</p>
    <p>第 {value.roundNumber} 轮 · 本轮检索 {value.roundPoiRequests}/{value.roundPoiLimit} 次 · 累计 {value.cumulativePoiRequests} 次</p>
    <p>路线核验已用 {value.routeRequests} 次，剩余 {value.routeRemaining} 次</p>
    {value.limitations.map(note => <p key={note}>{note}</p>)}
  </div>;
}
