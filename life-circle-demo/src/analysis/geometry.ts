import type { BaiduMapApi, BMapMap, BMapPolygonOptions } from '../map/baiduMapTypes';
import type { BusinessGeometry, Isochrone } from './types';
import type { Geometry } from '../api-contract';

export type DrawableGeometry = Pick<Geometry, 'type' | 'coordinates'> | BusinessGeometry;

export function geometryForMinutes(result: Isochrone, minutes: number): BusinessGeometry | null {
  const band = result.timeBands?.find(b => b.minutes === minutes);
  return band ? band.geometry : minutes === 15 ? result.geometry : null;
}

/** Each component keeps its exterior and all interior rings in one overlay. */
export function polygonPaths(geometry: DrawableGeometry | null): string[][] {
  if (!geometry) return [];
  const polygons = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates;
  return polygons.map(polygon => {
    if (!Array.isArray(polygon)) throw new Error('Invalid polygon');
    return polygon.map(ring => {
      if (!Array.isArray(ring)) throw new Error('Invalid ring');
      return ring.map(point => {
        if (!Array.isArray(point) || point.length !== 2 || !point.every(Number.isFinite)) throw new Error('Invalid point');
        return point.join(',');
      }).join(';');
    });
  });
}
export function drawGeometry(map: BMapMap, api: BaiduMapApi, geometry: DrawableGeometry | null, style: BMapPolygonOptions) {
  for (const rings of polygonPaths(geometry)) map.addOverlay(new api.Polygon(rings, style));
}
export function geometryMessage(geometry: BusinessGeometry | null) {
  if (geometry === null) return '证据不足，无法确定可达区域';
  if (!geometry.coordinates.length) return '有效证据范围内，可达区域为空';
  return `已重建 ${geometry.coordinates.length} 个可达分量`;
}
