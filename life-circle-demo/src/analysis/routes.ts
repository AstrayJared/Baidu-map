import { validRoute } from './validate';
import type { RouteEvidence, PoiEvidence } from '../api-contract';

export function poiStatusLabel(evidence: PoiEvidence | null | undefined) {
  return evidence ? { pending: '待核验（未知）', verified_reachable: '严格核验：15分钟内可达',
    verified_unreachable: '严格核验：返回路线超过15分钟' }[evidence.status] : '旧版结果未提供严格证据';
}

/** Parsed endpoints alone are not evidence that the requested route is valid. */
export function presentRoute(route: RouteEvidence) {
  const usable = route.endpoint_verified && route.reason === null && route.poiEvidence?.status !== 'pending'
    && route.duration_s !== null && route.distance_m !== null;
  return {
    path: usable ? route.path.map(([lng, lat]): [number, number] => [lng, lat]) : [],
    distance: usable ? route.distance_m : null,
    duration: usable ? route.duration_s : null,
    message: usable ? (route.poiEvidence ? poiStatusLabel(route.poiEvidence) : '路线可用；不代表设施已严格核验')
      : route.reason === 'endpoint_offset' ? '路线端点偏移，设施可达性未知'
        : '路线证据不足，设施可达性未知',
  };
}

/** 与 service.ts 相同的基址解析：未配置时同源请求，不硬编码端口。 */
export function routeApiBase(): string {
  return (import.meta.env.VITE_API_BASE_URL?.trim() || '').replace(/\/$/, '');
}

export async function requestFacilityRoute(taskId: string, facilityId: string, signal: AbortSignal,
  fetcher: typeof fetch = fetch) {
  const base = routeApiBase();
  const response = await fetcher(`${base}/api/analyses/${encodeURIComponent(taskId)}/routes/${encodeURIComponent(facilityId)}`, {
    method: 'POST', signal: AbortSignal.any([signal, AbortSignal.timeout(25_000)]),
  });
  if (!response.ok) throw new Error(response.status === 429
    ? '本次新增路线查询已达3次，请使用已有路线。'
    : response.status === 409
      ? '设施结果尚未就绪，无法查询路线。'
      : response.status === 404
        ? '设施不属于本次分析或任务已过期，请重新分析。'
        : '路线暂不可用，请重试。');
  const value: unknown = await response.json();
  if (!validRoute(value)) throw new Error('路线格式异常');
  if (value.poiEvidence && value.poiEvidence.facilityId !== facilityId) throw new Error('路线证据与所选设施不一致');
  signal.throwIfAborted();
  return { ...value, path: value.path.map(([lng, lat]): [number, number] => [lng, lat]) };
}
