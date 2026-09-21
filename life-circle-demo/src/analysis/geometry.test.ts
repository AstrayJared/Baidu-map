import { describe, expect, it } from 'vitest';
import { polygonPaths, geometryMessage, drawGeometry, drawOutline } from './geometry';
import type { BusinessGeometry } from './types';
import type { BaiduMapApi, BMapMap } from '../map/baiduMapTypes';

const geometry: BusinessGeometry = { type: 'MultiPolygon', coordinateSystem: 'bd09ll', coordinates: [
  [[[116, 39], [117, 39], [117, 40], [116, 39]], [[116.2, 39.2], [116.4, 39.2], [116.3, 39.4], [116.2, 39.2]]],
  [[[118, 40], [119, 40], [118, 41], [118, 40]]],
] };

describe('business geometry rendering', () => {
  it('renders only exterior outlines without changing calculation rings', () => {
    const before = JSON.stringify(geometry);
    const paths: unknown[] = [], styles: { fillOpacity: number }[] = [];
    const api = { Polygon: class { constructor(points: unknown, options: { fillOpacity: number }) {
      paths.push(points); styles.push(options);
    } } } as unknown as BaiduMapApi;
    const map = { addOverlay() {} } as unknown as BMapMap;
    drawOutline(map, api, geometry);
    expect(paths).toEqual(polygonPaths(geometry).map(rings => [rings[0]]));
    expect(styles.every(s => s.fillOpacity === 0)).toBe(true);
    drawOutline(map, api, null);
    drawOutline(map, api, { ...geometry, coordinates: [] });
    expect(paths).toHaveLength(2);
    expect(JSON.stringify(geometry)).toBe(before);
  });
  it('keeps every component and hole in BD09 coordinates', () => {
    expect(polygonPaths(geometry)).toEqual([
      ['116,39;117,39;117,40;116,39', '116.2,39.2;116.4,39.2;116.3,39.4;116.2,39.2'],
      ['118,40;119,40;118,41;118,40'],
    ]);
    const paths: unknown[] = [];
    const overlays: unknown[] = [];
    const api = { Polygon: class { constructor(points: unknown) { paths.push(points); } } } as unknown as BaiduMapApi;
    drawGeometry({ addOverlay: (overlay: unknown) => overlays.push(overlay) } as unknown as BMapMap, api, geometry, {});
    expect(paths).toEqual(polygonPaths(geometry));
    expect(overlays).toHaveLength(2);
  });
  it('distinguishes null and empty evidence without creating polygons', () => {
    expect(geometryMessage(null)).toContain('证据不足');
    expect(geometryMessage({ ...geometry, coordinates: [] })).toContain('为空');
    expect(polygonPaths(null)).toEqual([]);
  });
});
