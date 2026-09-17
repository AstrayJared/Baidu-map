import type { AnalysisResult, BusinessGeometry } from './types';

/** Offline contract fixture only; never used by the application. */
export function resultFixture(): AnalysisResult {
  const empty: BusinessGeometry = { type: 'MultiPolygon', coordinateSystem: 'bd09ll', coordinates: [] };
  return {
    schema_version: '1.0', responseType: 'result', taskStatus: 'completed', status: 'partial', businessStatus: 'partial',
    coordinateSystem: 'bd09ll', coordinateOrder: 'longitude,latitude',
    units: { distance: 'm', duration: 's', area: 'm2' },
    rules: { version: 'n04-v1', time_threshold_s: 900, time_inclusive: true, time_tolerance_s: 0,
      time_confirmation: 'implementation_only', distance: { metric: 'unconfirmed', threshold_m: 1000,
        inclusive: null, tolerance_m: 0, assessment_scope: 'unconfirmed', category_policy: 'unconfirmed' } },
    data: { geometry: null, evidenceGeometry: null, inferredRegion: null,
      uncertain_region: null, unknown_region: null, computation_extent: null,
      facilities: null, categories: [], blind_points: null, blind_region: null, report: null },
    algorithm: null, warnings: [], errors: [],
    taskId: 'one', center: { lng: 116.404, lat: 39.915 }, dataSource: 'synthetic',
    generatedAt: 1_789_200_000, facilitiesStatus: 'not_integrated',
    isochrone: {
      coordinateSystem: 'bd09ll', geometry: { ...empty, coordinates: [
        [[[116.4, 39.9], [116.5, 39.9], [116.5, 40], [116.4, 39.9]],
          [[116.42, 39.92], [116.44, 39.92], [116.44, 39.94], [116.42, 39.92]]],
        [[[116.6, 39.9], [116.7, 39.9], [116.7, 40], [116.6, 39.9]]],
      ] },
      unknownRegion: empty, uncertainRegion: empty, computationExtent: empty,
      quality: 'usable', stopReason: 'budget', warnings: [],
      statistics: { requests: 200, network_requests: 0, retries: 0, unknown_area: 0,
        unfinished_boundary: 0, total_seconds: 1, failures: {} },
      config: { origin: [116.404, 39.915], budget: 200, seed: 20260911,
        threshold: 900, coordinate_system: 'bd09ll' },
    },
  };
}
