/** 设施密度层的数据视图：谁参与热力，以及图例里必须一起说清的限制。
 *
 * 三条口径写在这里而不是散在组件里：
 *
 * * **只统计已确认在圈内的设施。** `in_circle === true` 是唯一的参与条件：本层的图例
 *   写的是"圈内设施等权去重"，把尚未判定（null）的设施也算进去，等于把未知当成圈内，
 *   图上会凭空多出一片密度。尚未判定的设施不消失 —— 它们仍留在列表、统计和报告里。
 * * **查询不完整就说出来。** 部分/截断的查询仍然可以画热力，但对照图例必须写明它
 *   只覆盖已检索到的设施，不能被读成圈内全部分布。
 * * **不参与的也要有个数。** 图例给出未判定条数，读者才知道图上少的那部分有多大。
 */
import type { Facility, FacilityAnalysis } from '../api-contract';
import { displayGroupForMajor } from '../taxonomy';

/** 该设施是否参与本层密度：大类和圈内状态都要过。 */
function participates(facility: Facility, group: string): boolean {
  const displayGroup = facility.displayGroup ?? displayGroupForMajor(facility.major_category);
  return (group === 'all' || displayGroup === group || facility.major_category === group) && facility.in_circle === true;
}

/** 参与密度层的设施：按大类和圈内状态筛选，去重在着色前由密度层完成。 */
export function heatPointsOf(facilities: readonly Facility[] | null | undefined, group = 'all'): Facility[] {
  return (facilities ?? []).filter(item => participates(item, group));
}

/** 因"尚未判定是否在圈内"而不参与密度层的设施数量；图例必须说出来。 */
export function undeterminedInCircleOf(facilities: readonly Facility[] | null | undefined, group = 'all'): number {
  return (facilities ?? []).filter(item =>
    (group === 'all' || item.displayGroup === group || displayGroupForMajor(item.major_category) === group
      || item.major_category === group) && item.in_circle === null).length;
}

/** 查询不完整时的数据范围提示；完整查询返回 null，不制造无谓的警告。 */
export function heatNoticeFor(analysis: FacilityAnalysis | null | undefined): string | null {
  if (!analysis) return '设施检索尚未接入本次结果，热力层没有真实设施参与。';
  const incomplete = analysis.queries.filter(query => query.status !== 'complete');
  if (analysis.status === 'complete' && incomplete.length === 0) return null;
  const kinds = [...new Set(incomplete.map(query => query.status))];
  const label: Record<string, string> = {
    partial: '部分完成', truncated: '被截断', failed: '失败', complete: '完成',
  };
  const detail = kinds.length ? `（${kinds.map(kind => label[kind] ?? kind).join('、')}）` : '';
  return `设施查询为${analysis.status === 'complete' ? '部分' : analysis.status}结果${detail}，热力只覆盖已检索到的设施，不代表圈内全部分布。`;
}
