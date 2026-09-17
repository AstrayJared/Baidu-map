import type { HybridRequest, HybridResultResponse, TaskStatusResponse } from '../api-contract';

type ObjectValue = Record<string, unknown>;
const object = (v: unknown): v is ObjectValue => v !== null && typeof v === 'object' && !Array.isArray(v);
const count = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v >= 0;
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const point = (v: unknown): boolean => Array.isArray(v) && v.length === 2 && finite(v[0]) && finite(v[1])
  && v[0] >= -180 && v[0] <= 180 && v[1] > -85 && v[1] < 85;
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(s => typeof s === 'string');

function geometry(v: unknown): boolean {
  if (v === null) return true;
  if (!object(v) || v.coordinateSystem !== 'bd09ll' || !Array.isArray(v.coordinates)) return false;
  const polygons = v.type === 'Polygon' ? [v.coordinates] : v.type === 'MultiPolygon' ? v.coordinates : null;
  return polygons !== null && polygons.every(p => Array.isArray(p) && p.length > 0 && p.every(r =>
    Array.isArray(r) && r.length >= 4 && r.every(point) && r[0][0] === r[r.length - 1][0] && r[0][1] === r[r.length - 1][1]));
}

export function validHybridTask(v: unknown): v is TaskStatusResponse {
  return object(v) && v.schema_version === '1.0' && v.responseType === 'task'
    && typeof v.taskId === 'string' && v.taskId.length > 0
    && ['running', 'cancelling', 'completed', 'cancelled', 'failed'].includes(v.status as string)
    && count(v.requests) && count(v.networkRequests) && count(v.budget) && v.budget >= 1 && v.budget <= 400
    && v.requests <= v.budget && v.networkRequests <= v.budget && finite(v.elapsedSeconds) && v.elapsedSeconds >= 0
    && typeof v.stage === 'string' && ['synthetic', 'baidu_walking'].includes(v.dataSource as string)
    && (v.error === null || typeof v.error === 'string')
    && (v.businessStatus === null || ['complete', 'partial', 'failed', 'empty'].includes(v.businessStatus as string));
}

export function validHybridResult(v: unknown): v is HybridResultResponse {
  if (!object(v) || v.schema_version !== '1.0' || v.responseType !== 'result' || v.taskStatus !== 'completed'
    || typeof v.taskId !== 'string' || !v.taskId || v.coordinateSystem !== 'bd09ll'
    || v.coordinateOrder !== 'longitude,latitude' || v.facilitiesStatus !== 'not_integrated'
    || !['partial', 'failed'].includes(v.status as string) || v.status !== v.businessStatus
    || !['synthetic', 'baidu_walking'].includes(v.dataSource as string)
    || !object(v.center) || !point([v.center.lng, v.center.lat]) || !finite(v.generatedAt)
    || !object(v.units) || v.units.distance !== 'm' || v.units.duration !== 's' || v.units.area !== 'm2'
    || !object(v.rules) || !object(v.data) || !Array.isArray(v.warnings) || !Array.isArray(v.errors)
    || !object(v.isochrone) || !object(v.algorithm)
    || ![v.config_hash, v.result_hash].every(s => typeof s === 'string' && /^[a-f0-9]{64}$/.test(s))) return false;
  const r = v.isochrone;
  if (r.algorithm !== 'hybrid' || r.algorithm_version !== 'hybrid-v1.5.0' || r.coordinate_system !== 'bd09ll'
    || !['usable', 'partial', 'insufficient'].includes(r.quality as string)
    || r.coverage_policy !== 'continuous_land_interior' || typeof r.extent_truncated !== 'boolean'
    || !strings(r.warnings) || typeof r.stop_reason !== 'string' || !object(r.config)
    || !object(r.readiness) || !['full', 'degraded'].includes(r.readiness.mode as string)
    || !strings(r.readiness.warnings) || !object(r.timing_seconds)) return false;
  const numericCounts = ['requests_used', 'valid_baidu_samples', 'invalid_baidu_samples', 'unknown_samples'];
  const geometries = ['geometry', 'evidence_geometry', 'inferred_fill_geometry', 'unknown_region', 'evidence_unknown_region', 'computation_extent'];
  const flags = ['graph_available', 'data_version_matches', 'coverage_available', 'origin_in_coverage',
    'extent_in_coverage', 'obstacle_layer_available', 'risk_layer_available'];
  const timings = ['preparation', 'obstacle_load', 'requests', 'geometry_rebuilds', 'compute_total', 'task_total'];
  return numericCounts.every(k => count(r[k])) && (r.requests_used as number) <= 400
    && r.config.time_limit_seconds === 900 && finite(r.config.analysis_half_width_m)
    && r.config.analysis_half_width_m > 0 && r.config.analysis_half_width_m <= 1200
    && count(r.config.max_baidu_requests) && r.config.max_baidu_requests >= (r.requests_used as number)
    && r.config.max_baidu_requests <= 400 && geometries.every(k => geometry(r[k])) && r.computation_extent !== null
    && flags.every(k => typeof (r.readiness as ObjectValue)[k] === 'boolean')
    && timings.every(k => finite((r.timing_seconds as ObjectValue)[k]) && ((r.timing_seconds as ObjectValue)[k] as number) >= 0)
    && (r.boundary_error_estimate === null || (finite(r.boundary_error_estimate) && r.boundary_error_estimate >= 0))
    && JSON.stringify(v.algorithm) === JSON.stringify(r);
}

export class HybridApiError extends Error {
  constructor(public readonly code: string, public readonly status: number) { super(code); }
}

// The production analysis uses the dedicated Hybrid contract.
export function createHybridClient(base = import.meta.env.VITE_API_BASE_URL?.trim() || '', fetcher: typeof fetch = fetch) {
  const prefix = `${base.replace(/\/$/, '')}/api/v1/analysis/hybrid`;
  async function request<T>(path: string, validate: (v: unknown) => v is T, body?: unknown, signal?: AbortSignal): Promise<T> {
    const response = await fetcher(`${prefix}${path}`, { method: body === undefined ? 'GET' : 'POST',
      ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000) });
    const value: unknown = await response.json();
    if (!response.ok) throw new HybridApiError(object(value) && typeof value.code === 'string' ? value.code : 'hybrid_http_error', response.status);
    if (!validate(value)) throw new HybridApiError('hybrid_invalid_response', response.status);
    return value;
  }
  return {
    create: (input: HybridRequest) => request('', validHybridTask, input),
    status: (id: string, signal?: AbortSignal) => request(`/${encodeURIComponent(id)}`, validHybridTask, undefined, signal),
    result: async (id: string, signal?: AbortSignal) => {
      const value = await request(`/${encodeURIComponent(id)}/result`, validHybridResult, undefined, signal);
      if (value.taskId !== id) throw new HybridApiError('hybrid_task_mismatch', 200);
      return value;
    },
    cancel: (id: string) => request(`/${encodeURIComponent(id)}/cancel`, validHybridTask, {}),
    byRequest: (id: string) => request(`/by-request/${encodeURIComponent(id)}`, validHybridTask),
    cancelByRequest: (id: string) => request(`/by-request/${encodeURIComponent(id)}/cancel`, validHybridTask, {}),
  };
}
