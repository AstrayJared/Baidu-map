import { categories, categoryMeta } from '../types';
import type { AnalysisInput, AnalysisResult } from './types';
import { validResult } from './validate';
import { geometryMessage } from './geometry';
import type { RouteEvidence } from '../api-contract';

/** Enrich only the existing task snapshot; route evidence cannot change geometry/counts. */
export function withFacilityRoute(result: AnalysisResult, route: RouteEvidence): AnalysisResult {
  const evidence = route.poiEvidence;
  if (!evidence || !result.facilityAnalysis) return result;
  const facility = result.data.facilities?.find(f => f.id === evidence.facilityId);
  if (!facility || evidence.requestOrigin[0] !== result.center.lng || evidence.requestOrigin[1] !== result.center.lat
    || evidence.destination[0] !== +facility.location.lng.toFixed(6)
    || evidence.destination[1] !== +facility.location.lat.toFixed(6)) throw new Error('路线证据与分析条件不一致');
  const copy = structuredClone(result);
  copy.data.facilities!.find(f => f.id === facility.id)!.poiEvidence = structuredClone(evidence);
  copy.facilityAnalysis!.routes[facility.id] = structuredClone(route);
  return copy;
}

/** Task API → geographic frontend AnalysisResult. Never use the legacy demo projection. */
export function decodeAnalysisResult(value: unknown): AnalysisResult {
  if (!validResult(value)) throw new Error('后端返回格式异常，请检查服务版本');
  const keys = ['schema_version', 'responseType', 'taskId', 'taskStatus', 'status', 'businessStatus',
    'dataSource', 'center', 'generatedAt', 'facilitiesStatus', 'facilityAnalysis', 'coordinateSystem',
    'coordinateOrder', 'units', 'rules', 'data', 'algorithm', 'warnings', 'errors', 'isochrone'] as const;
  return structuredClone(Object.fromEntries(keys.filter(k => k in value).map(k => [k, value[k]]))) as AnalysisResult;
}

export function matchesAnalysisInput(result: AnalysisResult, input: AnalysisInput) {
  // Backend normalizes the requested center to six decimal places.
  return result.center.lng === +input.center.lng.toFixed(6)
    && result.center.lat === +input.center.lat.toFixed(6)
    && result.isochrone.config.budget === input.budget;
}

/** Completion is a task state; it does not mean every business module has run. */
export function analysisAvailability(result: AnalysisResult): 'partial' | 'unavailable' {
  return result.status === 'failed' || result.isochrone.quality === 'insufficient' || result.isochrone.geometry === null
    ? 'unavailable' : 'partial';
}

export function analysisReportView(result: AnalysisResult) {
  return {
    taskId: result.taskId, center: result.center,
    generatedAt: new Date(result.generatedAt * 1000).toISOString(),
    dataSource: result.dataSource === 'synthetic' ? '合成时间场（非真实社区）'
      : '百度步行路线数据',
    availability: analysisAvailability(result),
    geometrySummary: geometryMessage(result.isochrone.geometry),
    qualityLabel: { usable: '可用', partial: '部分结果', insufficient: '证据不足' }[result.isochrone.quality],
    budget: result.isochrone.config.budget,
    statistics: result.isochrone.statistics,
    warnings: result.isochrone.warnings,
    // not_integrated is unknown, never zero facilities or zero blind zones.
    facilityStats: categories.map(category => {
      const entry = result.data.categories.find(c => c.category === category);
      return { category, label: categoryMeta[category].label,
        count: result.facilityAnalysis ? entry?.count_in_circle ?? null : null,
        state: result.facilityAnalysis ? '检索记录 · 估算圈内' : '尚未接入' };
    }),
    blindZoneCount: null,
    poiCounts: {
      reachable: (result.data.facilities ?? []).filter(f => f.poiEvidence?.status === 'verified_reachable').length,
      unreachable: (result.data.facilities ?? []).filter(f => f.poiEvidence?.status === 'verified_unreachable').length,
      pending: (result.data.facilities ?? []).filter(f => f.poiEvidence?.status === 'pending').length,
      legacy: (result.data.facilities ?? []).filter(f => !f.poiEvidence).length,
    },
  };
}
