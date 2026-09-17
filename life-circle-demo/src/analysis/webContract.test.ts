import { expect, it } from 'vitest';
import { analysisAvailability, decodeAnalysisResult } from './adapter';
import { geometryForMinutes } from './geometry';
import { presentRoute } from './routes';
import { validRoute, validTask } from './validate';
import { resultFixture } from './testFixtures';

const route = { endpoint_verified: true, distance_m: 600, duration_s: 500, reason: null,
  path: [[116.404, 39.915], [116.405, 39.916]] };

it('parsed endpoints with an offset remain unknown; observed duration never rescues the requested route', () => {
  const shifted = { ...route, duration_s: null, reason: 'endpoint_offset', observed_duration: 500,
    origin_offset_m: 0, destination_offset_m: 70 };
  expect(validRoute(shifted)).toBe(true);
  expect(presentRoute(shifted)).toEqual({ path: [], distance: null, duration: null,
    message: '路线端点偏移，设施可达性未知' });
  expect(presentRoute(route).path).toEqual(route.path);
  expect(presentRoute(route).message).not.toContain('端点已核验');
});

it.each(['endpoint_offset', 'timeout', 'no_route'])('rejects duration with failure reason %s', reason => {
  expect(validRoute({ ...route, reason })).toBe(false);
  expect(validRoute({ ...route, reason, duration_s: null, path: [] })).toBe(true);
});

it('does not publish failed business results even when transport and geometry succeeded', () => {
  const result = resultFixture();
  result.status = result.businessStatus = 'failed';
  expect(analysisAvailability(decodeAnalysisResult(result))).toBe('unavailable');
});

it('keeps unknown and unreachable regions separate and never substitutes a missing time band', () => {
  const wire = resultFixture();
  wire.isochrone.unreachableRegion = structuredClone(wire.isochrone.geometry);
  const result = decodeAnalysisResult(wire).isochrone;
  expect(result.unknownRegion.coordinates).toEqual([]);
  expect(result.unreachableRegion?.coordinates.length).toBe(2);
  expect(geometryForMinutes(result, 5)).toBeNull();
  expect(geometryForMinutes(result, 15)).toEqual(result.geometry);
  result.timeBands = [{ minutes: 15, geometry: null }, { minutes: 5, geometry: result.unknownRegion }];
  expect(geometryForMinutes(result, 15)).toBeNull();
  expect(geometryForMinutes(result, 5)?.coordinates).toEqual([]);
});

it('rejects category contract drift before report rendering', () => {
  const wire = resultFixture();
  expect(() => decodeAnalysisResult({ ...wire, data: { ...wire.data, categories: null } })).toThrow();
  expect(() => decodeAnalysisResult({ ...wire, generatedAt: 0 })).toThrow();
});

it('requires the task envelope, stage string, supported budget and state enum', () => {
  const task = { schema_version: '1.0', responseType: 'task', taskId: 'one', status: 'running',
    businessStatus: null, stage: 'refining', requests: 10, networkRequests: 0, budget: 200,
    elapsedSeconds: 1, dataSource: 'synthetic', error: null };
  expect(validTask(task)).toBe(true);
  for (const drift of [{ taskId: '' }, { stage: null }, { status: 'done' }, { budget: 250 }]) {
    expect(validTask({ ...task, ...drift })).toBe(false);
  }
  expect(() => decodeAnalysisResult({ ...resultFixture(), coordinateSystem: 'wgs84' })).toThrow();
});
