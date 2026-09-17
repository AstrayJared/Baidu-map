import { expect, it } from 'vitest';
import type { PoiEvidence, RouteEvidence } from '../api-contract';
import { validPoiEvidence, validResult, validRoute } from './validate';
import { poiStatusLabel, presentRoute, requestFacilityRoute } from './routes';
import { withFacilityRoute, analysisReportView } from './adapter';
import { resultFixture } from './testFixtures';

const evidence: PoiEvidence = { version: '1.0', facilityId: 'poi', status: 'verified_reachable',
  reason: null, duration: 500, observedDuration: 500, endpointVerified: true,
  requestOrigin: [116.404, 39.915], destination: [116.405, 39.915],
  routeOrigin: [116.404, 39.915], routeDestination: [116.405, 39.915],
  originOffsetM: 0, destinationOffsetM: 0 };
const route: RouteEvidence = { distance_m: 600, duration_s: 500, endpoint_verified: true, reason: null,
  path: [evidence.requestOrigin, evidence.destination], poiEvidence: evidence };

it.each([
  { ...evidence },
  { ...evidence, status: 'verified_unreachable', duration: 901, observedDuration: 901 },
  { ...evidence, status: 'pending', reason: 'endpoint_offset', duration: null, destinationOffsetM: 80,
    routeDestination: [116.406, 39.915] },
])('accepts consistent explicit backend state $status', value => {
  expect(validPoiEvidence(value)).toBe(true);
});

it.each([{ status: 'unknown' }, { reason: 'timeout' }, { duration: 901 }, { endpointVerified: false },
  { observedDuration: 10 }, { destinationOffsetM: 1 }, { requestOrigin: [116.403, 39.915] },
  { routeDestination: [116.406, 39.915], destinationOffsetM: null }, { version: '2.0' }])('rejects conflicting proof %j', change => {
  expect(validPoiEvidence({ ...evidence, ...change })).toBe(false);
});

it('legacy evidence remains unsupported, not fabricated pending', () => {
  expect(poiStatusLabel(undefined)).toBe('旧版结果未提供严格证据');
  expect(poiStatusLabel(null)).toBe('旧版结果未提供严格证据');
});

it('a diagnostic duration and parsed endpoints cannot turn pending into a route', () => {
  const value = { ...route, poiEvidence: { ...evidence, status: 'pending' as const,
    duration: null, reason: 'endpoint_mapping_unconfirmed', destinationOffsetM: 1 } };
  expect(presentRoute(value).path).toEqual([]);
  expect(poiStatusLabel(value.poiEvidence)).toBe('待核验（未知）');
});

it('enriches a copy of the same task snapshot without changing geometry or business counts', () => {
  const result = resultFixture();
  result.facilitiesStatus = 'partial';
  result.data.report = '离线证据快照测试';
  result.data.facilities = [{ id: 'poi', name: '测试药房', category: 'pharmacy', minor_category: 'pharmacy',
    major_category: 'medical', location: { lng: 116.405, lat: 39.915 }, in_circle: null }];
  result.facilityAnalysis = { status: 'partial', queries: [], assessments: [], candidate_points: 0,
    assessed_points: 0, unassessed_points: 0, network_requests: 0, elapsed_seconds: 0,
    search_radius_m: 3500, routes: {}, serviceBlindRegions: {}, warnings: [] };
  const next = withFacilityRoute(result, route);
  expect(result.data.facilities[0].poiEvidence).toBeUndefined();
  expect(next.isochrone).toEqual(result.isochrone);
  expect(next.data.categories).toEqual(result.data.categories);
  expect(next.data.facilities![0].poiEvidence).toEqual(next.facilityAnalysis!.routes.poi.poiEvidence);
  expect(analysisReportView(next).poiCounts).toEqual({ reachable: 1, unreachable: 0, pending: 0, legacy: 0 });
  expect(validResult(next)).toBe(true);
  const mismatched = structuredClone(next);
  mismatched.data.facilities![0].poiEvidence = null;
  expect(validResult(mismatched)).toBe(false);
  expect(() => withFacilityRoute({ ...result, center: { lng: 120, lat: 30 } }, route)).toThrow('分析条件');
});

it('rejects legacy route duration contradicting new strict proof', () => {
  expect(validRoute({ ...route, duration_s: 600 })).toBe(false);
});

it('rejects route proof for a different selected facility', async () => {
  await expect(requestFacilityRoute('task', 'another', new AbortController().signal,
    async () => new Response(JSON.stringify(route)))).rejects.toThrow('所选设施');
});
