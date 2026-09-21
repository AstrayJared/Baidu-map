import { Tag } from 'antd';
import { isAnalysisBusy, type AnalysisState } from './types';
import styles from '../components/loading.module.css';

const stages: Record<string, string> = {
  initializing: '初始化采样', expanding: '检查并扩展范围', exploring: '探索采样',
  refining: '边界细化与补测', reconstructing: '重建边界几何', facilities: '检索设施与核对步行距离',
};

/** Reuse the demo's visual primitives, without its timer, percentage or facility claims. */
export function AnalysisProgress({ state }: { state: AnalysisState }) {
  const busy = isAnalysisBusy(state);
  const { task, phase } = state;
  if (!busy && phase !== 'completed') return null;
  const title = phase === 'submitting' ? '正在创建任务'
    : phase === 'fetching' ? '正在整理结果'
      : phase === 'cancelling' ? '正在取消任务'
        : phase === 'completed' ? '分析完成'
          : stages[task?.stage ?? ''] || '正在计算等时圈';
  return <div className="api-progress" data-testid="analysis-progress" role="status" aria-live="polite" aria-atomic="true" aria-busy={busy}>
    {busy && <div className={styles.stageVisual} aria-hidden="true">
      <div className={`${styles.radar} ${styles.radarOn}`} /><div className={styles.pinCore} />
    </div>}
    <p className={styles.stageTitle}>{title}</p>
    {task && <>
      <Tag color={task.dataSource === 'synthetic' ? 'orange' : 'green'}>{task.dataSource === 'synthetic' ? '合成数据 · 离线验收' : '百度步行数据'}</Tag>
      <p><strong>{task.requests} / {task.budget}</strong> 次调用</p>
      <p>已用时 {task.elapsedSeconds.toFixed(1)} 秒</p>
    </>}
    {busy && <p>阶段与计数来自实际请求；调用预算不代表完成百分比。</p>}
  </div>;
}
