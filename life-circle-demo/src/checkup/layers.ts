/**
 * v2 图层 → 可绘制的形状与点。
 *
 * 纯函数，不认识地图：一份图层响应进来，一组"画什么、用什么样式、带什么属性"出去。
 * 地图组件只负责把它们交给 SDK，因此这里的每一条规则都能在没有浏览器的情况下测。
 *
 * 三条规矩：
 *
 * - **没取到的图层和取到但为空的图层不是一回事**，用 `state` 分开表达：前者界面要说
 *   "尚未加载"，后者要说"这次没有内容"。把它们都画成"什么都没有"，读者会以为已经查过了。
 * - **画的是后端给的几何，不做任何裁剪或简化**：灰区面积是按同一份几何算的，图上少一块
 *   就与报告里的数字对不上。
 * - **点按属性着色**，颜色表只有这一处；图例、地图、面板共用它，才不会出现"图上是医疗蓝、
 *   图例写的购物绿"。
 */
import type { CheckupLayer } from './contract';
import { CATEGORY_COLORS } from './report';
import { readLayerGeometry, type LayerId } from './validate';
import type { DrawableGeometry } from '../analysis/geometry';
import type { ServiceSample, ServiceStatus } from '../map/layers/serviceField';
import { categoryLabel } from './report';

export type LayerStyle = {
  label: string;
  strokeColor: string;
  fillColor: string;
  fillOpacity: number;
  strokeWeight: number;
  strokeStyle?: 'solid' | 'dashed';
  /** 只有边界、没有面积含义的图层（评估域、等时圈轮廓）在这里说明怎么读。 */
  note: string;
};

export const LAYER_STYLES: Record<LayerId, LayerStyle> = {
  isochrone: { label: '步行等时圈', strokeColor: '#3366ff', fillColor: '#3366ff',
    fillOpacity: 0.26, strokeWeight: 2, note: '按相同步行预算圈出的边界，不是服务覆盖结论。' },
  accessibility: { label: '评估域', strokeColor: '#8a94a6', fillColor: '#8a94a6',
    fillOpacity: 0, strokeWeight: 1, strokeStyle: 'dashed',
    note: '所有面积比例的分母。域外一律不评估，也不计入覆盖率。' },
  service_gaps: { label: '服务灰区', strokeColor: '#5c6370', fillColor: '#8a94a6',
    fillOpacity: 0.38, strokeWeight: 1, note: '步行超出服务标准的连片区域，按面积降序编号。' },
  facilities: { label: '设施点位', strokeColor: '#ffffff', fillColor: '#8a94a6',
    fillOpacity: 1, strokeWeight: 2, note: '检索并被接受的设施；审核候选与隔离记录不上图。' },
  heatmap: { label: '模型网格采样', strokeColor: '#ffffff', fillColor: '#ff9f1a',
    fillOpacity: 1, strokeWeight: 2, note: '路网模型估计的最近设施距离与覆盖状态，不是百度路线实测。' },
  verification: { label: '核验设施', strokeColor: '#ffffff', fillColor: '#3366ff',
    fillOpacity: 1, strokeWeight: 2, note: '问过路的那几家设施及其结论。' },
  report: { label: '体检报告', strokeColor: '#3366ff', fillColor: '#3366ff',
    fillOpacity: 0, strokeWeight: 0, note: '报告是文档，没有形状。' },
};

/** 点色：设施与热力按大类，核验按结论。缺的属性给中性灰，不猜。 */
export const POINT_COLORS: Record<string, string> = {
  ...CATEGORY_COLORS,
  covered: '#3366ff', gap: '#ff8a00', unknown: '#8a94a6',
  verified_reachable: '#3366ff', verified_unreachable: '#ff8a00', pending: '#8a94a6',
};

export type LayerShape = { key: string; geometry: DrawableGeometry;
  properties: Record<string, unknown>; style: LayerStyle };
export type LayerPoint = { key: string; lng: number; lat: number; color: string;
  title: string; properties: Record<string, unknown> };

export type LayerDrawable = {
  state: 'absent' | 'empty' | 'ready';
  shapes: LayerShape[];
  points: LayerPoint[];
  /** 这一层的说明（读法与限制），由样式表给。 */
  note: string;
};

function pointOf(feature: Record<string, unknown>) {
  const geometry = feature.geometry as { coordinates: number[] };
  return { lng: geometry.coordinates[0], lat: geometry.coordinates[1] };
}

function propertiesOf(feature: Record<string, unknown>): Record<string, unknown> {
  return (feature.properties ?? {}) as Record<string, unknown>;
}

/** 点的颜色与提示：三类点各有自己的关键字段，缺字段时的说法必须能看出是缺字段。 */
function pointView(layerId: LayerId, index: number, feature: Record<string, unknown>): LayerPoint {
  const properties = propertiesOf(feature);
  const { lng, lat } = pointOf(feature);
  const pick = (...keys: string[]) => {
    for (const key of keys) {
      const value = properties[key];
      if (typeof value === 'string' && value) return value;
    }
    return null;
  };
  const classification = pick('poiStatus', 'majorCategory', 'status');
  // 设施点的 majorCategory 是机器键（例如 finance/public），标题给读者看中文标签，
  // 但颜色仍按原始键取，避免把状态值（covered、verified_reachable）误当分类翻译。
  const classificationLabel = classification !== null && properties.majorCategory === classification
    ? categoryLabel(classification)
    : classification;
  const name = pick('name', 'facilityId', 'cell');
  const detail = pick('category', 'distanceM');
  const detailLabel = detail !== null && properties.category === detail ? categoryLabel(detail) : detail;
  const distance = properties.distanceM;
  const title = [name ?? `第 ${index + 1} 个点`, classificationLabel ?? '未分类',
    typeof distance === 'number' ? `${distance.toFixed(0)} 米` : detailLabel].filter(Boolean).join(' · ');
  return {
    key: layerId === 'heatmap' ? `${layerId}:${properties.category}:${properties.cell ?? index}`
      : String(properties.id ?? properties.facilityId ?? `${layerId}-${index}`),
    lng, lat,
    color: (classification && POINT_COLORS[classification]) ?? LAYER_STYLES[layerId].fillColor,
    title, properties,
  };
}

/**
 * 一层图层 → 可绘制内容。
 *
 * `layer` 为 `undefined` 表示**这次还没取**，与"取回来是空的"分开表达：地图上都画不出
 * 东西，但面板上必须说得不一样。
 */
export function drawableLayer(layerId: LayerId, layer: CheckupLayer | undefined): LayerDrawable {
  const style = LAYER_STYLES[layerId];
  if (!layer) return { state: 'absent', shapes: [], points: [], note: style.note };
  const geometry = readLayerGeometry(layer);
  if (geometry === null) throw new Error(`图层的几何无法解析：${layerId}`);
  if (geometry.kind === 'document') return { state: 'ready', shapes: [], points: [], note: style.note };
  if (geometry.kind === 'none') return { state: 'empty', shapes: [], points: [], note: style.note };
  if (geometry.kind === 'polygon') {
    return { state: 'ready', note: style.note,
      shapes: [{ key: layerId, geometry: geometry.geometry as DrawableGeometry,
        properties: {}, style }], points: [] };
  }
  const shapes: LayerShape[] = [];
  const points: LayerPoint[] = [];
  geometry.features.forEach((feature, index) => {
    const shape = feature.geometry as { type: string };
    if (shape.type === 'Point') {
      points.push(pointView(layerId, index, feature));
      return;
    }
    const properties = propertiesOf(feature);
    shapes.push({
      key: String(properties.id ?? `${layerId}-${index}`),
      geometry: shape as DrawableGeometry,
      // 灰区的颜色按"有没有完成设施检索"分：检索没跑完的灰区不是同一种结论。
      properties,
      style: properties.queryStatus === 'partial'
        ? { ...style, strokeColor: '#ff8a00', fillColor: '#ff8a00', fillOpacity: 0.28 } : style,
    });
  });
  return { state: shapes.length + points.length === 0 ? 'empty' : 'ready', shapes, points,
    note: style.note };
}

/** 后端网格的基准步长（米）；图层没报 stepM 时用它。它只决定平滑带宽，不改变任何格的值。 */
const DEFAULT_STEP_M = 50;
const SERVICE_STATUSES = new Set(['covered', 'gap', 'unknown']);

export type ServiceSamples = {
  samples: ServiceSample[];
  /** 评估域：域内没有格的地方也是未知，不能画成"什么都没有"。 */
  domain: { type: string; coordinates: unknown } | null;
  /** 格点坐标、结论或格编号不可用而被丢弃的个数；不为 0 时界面要说出来。 */
  dropped: number;
};

/**
 * 模型网格图层 → 服务覆盖热力的输入。
 *
 * 格编号是 `层级:ix:iy`，层级 k 的格边长是 stepM / 2^k：边长决定插值核的地面半径，
 * 细分过的格核更小，粗格与细格混排时各自只影响自己一格宽的范围。
 */
export function serviceSamples(layer: CheckupLayer | undefined): ServiceSamples {
  const empty: ServiceSamples = { samples: [], domain: null, dropped: 0 };
  if (!layer) return empty;
  const geometry = readLayerGeometry(layer);
  if (!geometry || geometry.kind !== 'collection') return empty;
  const stepM = typeof geometry.properties.stepM === 'number' && geometry.properties.stepM > 0
    ? geometry.properties.stepM : DEFAULT_STEP_M;
  const domainValue = geometry.properties.domain as { type?: unknown; coordinates?: unknown } | undefined;
  const domain = domainValue && (domainValue.type === 'Polygon' || domainValue.type === 'MultiPolygon')
    ? { type: domainValue.type, coordinates: domainValue.coordinates } : null;
  const samples: ServiceSample[] = [];
  let dropped = 0;
  for (const feature of geometry.features) {
    const shape = feature.geometry as { type: string };
    if (shape.type !== 'Point') continue;
    const properties = propertiesOf(feature);
    const { lng, lat } = pointOf(feature);
    const level = typeof properties.cell === 'string' ? Number(properties.cell.split(':')[0]) : NaN;
    const status = properties.status;
    const distance = properties.distanceM;
    if (!Number.isInteger(level) || level < 0 || typeof properties.category !== 'string'
      || typeof status !== 'string' || !SERVICE_STATUSES.has(status)
      || !Number.isFinite(lng) || !Number.isFinite(lat)) {
      dropped++;
      continue;
    }
    samples.push({ lng, lat, category: properties.category, status: status as ServiceStatus,
      distanceM: typeof distance === 'number' && Number.isFinite(distance) ? distance : null,
      sizeM: stepM / 2 ** level });
  }
  return { samples, domain, dropped };
}

/** 设施密度看哪一类；「全部」即三类合在一起算。 */
export const DENSITY_ALL = 'all';

export type DensityFacilities = {
  /** 交给密度模块的点：一组疑似重复只留一个，id 用组号。 */
  points: { id: string; lng: number; lat: number }[];
  /** 本类（筛选后、合并前）的已接收记录数。 */
  records: number;
  /** 因同一 UID 或同一疑似重复组而合并掉的记录数。 */
  merged: number;
};

/**
 * 设施图层 → 设施密度的输入。
 *
 * - 按大类筛选；「全部」不筛。缺大类的记录只在「全部」里出现，不猜它属于哪一类。
 * - 后端把同名同址、相距 20 米内的记录标成同一个疑似重复组：两个 UID 指的是同一家店时，
 *   密度不该因此翻倍，所以一组只算一个，位置取组内第一条（后端输出顺序固定）。
 * - 只读后端接收的设施：审核候选与隔离记录本来就不在这一层。
 */
export function densityFacilities(drawable: LayerDrawable | undefined, category: string = DENSITY_ALL): DensityFacilities {
  const points: DensityFacilities['points'] = [];
  const seen = new Set<string>();
  let records = 0;
  let merged = 0;
  for (const point of drawable?.points ?? []) {
    if (category !== DENSITY_ALL && point.properties.majorCategory !== category) continue;
    if (!Number.isFinite(point.lng) || !Number.isFinite(point.lat)) continue;
    records++;
    const group = point.properties.possibleDuplicateGroup;
    const id = typeof group === 'string' && group ? group : point.key;
    if (seen.has(id)) { merged++; continue; }
    seen.add(id);
    points.push({ id, lng: point.lng, lat: point.lat });
  }
  return { points, records, merged };
}
