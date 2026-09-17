import type { BaiduMapApi, BMapMap, BMapPolygonOptions } from '../map/baiduMapTypes';
import type { BusinessGeometry, Isochrone } from './types';

export function geometryForMinutes(result: Isochrone, minutes: number): BusinessGeometry | null {
  const band = result.timeBands?.find(b => b.minutes === minutes);
  return band ? band.geometry : minutes === 15 ? result.geometry : null;
}

/** Each component keeps its exterior and all interior rings in one overlay. */
export function polygonPaths(geometry: BusinessGeometry | null): string[][] {
  return geometry?.coordinates.map(polygon => polygon.map(ring => ring.map(point => point.join(',')).join(';'))) ?? [];
}
export function drawGeometry(map: BMapMap, api: BaiduMapApi, geometry: BusinessGeometry | null, style: BMapPolygonOptions) {
  for (const rings of polygonPaths(geometry)) map.addOverlay(new api.Polygon(rings, style));
}
export function geometryMessage(geometry: BusinessGeometry | null) {
  if (geometry === null) return '证据不足，无法确定可达区域';
  if (!geometry.coordinates.length) return '有效证据范围内，可达区域为空';
  return `已重建 ${geometry.coordinates.length} 个可达分量`;
}
