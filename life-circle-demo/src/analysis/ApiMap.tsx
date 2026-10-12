import { useEffect, useMemo, useRef, useState } from 'react';
import type { Center } from '../types';
import { useBaiduMap } from '../map/useBaiduMap';
import type { BMapIcon, BMapMap, BMapOverlay, BMapViewEventType } from '../map/baiduMapTypes';
import { createDotIcon } from '../map/mapIcons';
import { drawGeometry, drawOutline, type DrawableGeometry } from './geometry';
import {
  DENSITY_SCALE_MAX, DENSITY_UNIT, HEAT_KERNEL_RADIUS_M, SINGLE_FACILITY_PEAK, densityLegendCss,
} from '../map/layers/density';
import { aggregateByCell, FACILITY_CLUSTER_CELL_PX } from '../map/layers/aggregate';
import { createDensityOverlay, type DensityOverlay } from '../map/layers/heatmapOverlay';
import type { Facility, AssessmentPoint } from '../api-contract';
import { displayGroupForMajor, displayGroupMeta, majorMeta } from '../taxonomy';

export type Layers = { reachable: boolean; unreachable: boolean; unknown: boolean; uncertain: boolean; extent: boolean; serviceBlind: boolean; heatmap: boolean };

export type MapResult = {
  outlineOnly?: boolean;
  displayGeometry?: DrawableGeometry | null;
  geometry: DrawableGeometry | null;
  unknownRegion: DrawableGeometry | null;
  uncertainRegion: DrawableGeometry | null;
  computationExtent: DrawableGeometry | null;
  unreachableRegion?: DrawableGeometry | null;
  timeBands?: { minutes: number; geometry: DrawableGeometry | null }[];
};

/** 设施展示组颜色（与图例、FacilityPanel 分组一致）；符号取小类首字。 */
const displayGroupColors: Record<string, string> = {
  healthcare: '#397ac6', education: '#c78b36', daily_life: '#168875',
  public_mobility: '#64748b', leisure: '#2f855a',
};
const majorColors: Record<string, string> = Object.fromEntries(
  Object.keys(majorMeta).map(major => [major, displayGroupColors[displayGroupForMajor(major) ?? 'daily_life']]),
);
const minorSymbols: Record<string, string> = {
  market: '菜', supermarket: '超', pharmacy: '药', hospital_pharmacy: '医', school: '学', primary_school: '学',
  hospital: '院', clinic: '诊', checkup: '检', wet_market: '市', fresh_store: '鲜', combined_school: '学',
  middle_school: '中', college: '大', preschool: '幼', training: '培', nursing_home: '养', rehab: '康',
  restaurant: '餐', cafe: '饮', bank: '银', finance_service: '金', government: '政', post: '邮', library: '文',
  park: '园', sports: '体', entertainment: '娱', bus: '交', parking: '停', fuel: '能', repair: '修', beauty: '美',
};
const minorMajor: Record<string, string> = {
  pharmacy: 'medical', hospital_pharmacy: 'medical', hospital: 'medical', clinic: 'medical', checkup: 'medical',
  market: 'shopping', wet_market: 'shopping', fresh_store: 'shopping', supermarket: 'shopping',
  school: 'education', primary_school: 'education', combined_school: 'education', middle_school: 'education', college: 'education', preschool: 'education', training: 'education',
  nursing_home: 'care', rehab: 'care', restaurant: 'dining', cafe: 'dining', bank: 'finance', finance_service: 'finance',
  government: 'public', post: 'public', library: 'public', park: 'leisure', sports: 'leisure', entertainment: 'leisure',
  bus: 'transport', parking: 'transport', fuel: 'transport', repair: 'life', beauty: 'life',
};
const VIEW_EVENTS: BMapViewEventType[] = ['moveend', 'zoomend', 'resize'];

/** 覆盖物按图层组记账：每组只摘自己上一次加进去的那批。 */
type OverlayGroup = 'vector' | 'facility' | 'label';

/** 合并标记的主色取成员里最多数的大类，混合格不会因为第一条是药店就整格涂成医疗色。 */
function dominantMajor(items: readonly Facility[]): string {
  const counts = new Map<string, number>();
  for (const item of items) counts.set(item.major_category, (counts.get(item.major_category) ?? 0) + 1);
  let best: string = items[0]?.major_category ?? '';
  for (const [major, count] of counts) if (count > (counts.get(best) ?? 0)) best = major;
  return best;
}

export function ApiMap({ center, result, resultCenter, layers, onPick, minutes = 15, facilities = [], heatPoints = [], heatNotice = null, heatUndetermined = 0, assessments = [], blindRegions = {}, route = [], selected, onFacility }: { center: Center; result?: MapResult; resultCenter?: Center; layers: Layers; onPick: (center: Center) => void; minutes?: number; facilities?: Facility[]; heatPoints?: Facility[]; heatNotice?: string | null; heatUndetermined?: number; assessments?: AssessmentPoint[]; blindRegions?: Record<string, unknown>; route?: [number, number][]; selected?: string | null; onFacility?: (id: string) => void }) {
  const { api, mode, failureReason } = useBaiduMap();
  const container = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<BMapMap | null>(null);
  const pick = useRef(onPick);
  pick.current = onPick;
  // 回调走 ref：父组件每次渲染都会新建函数，直接进依赖会让设施标记反复重建。
  const facilityClick = useRef(onFacility);
  facilityClick.current = onFacility;
  const initialCenter = useRef(center);
  initialCenter.current = center;
  const [error, setError] = useState(false);
  // 热力层的生命周期独立于矢量图层：矢量每次全量重绘，热力只更新数据与裁剪面。
  const heat = useRef<DensityOverlay | null>(null);
  const heatSupported = Boolean(api?.Overlay);
  // 视图变化只影响需要按屏幕像素定位的图层（设施聚合标记），不触发矢量面重绘。
  const [viewTick, setViewTick] = useState(0);
  const overlays = useRef<Record<OverlayGroup, BMapOverlay[]>>({ vector: [], facility: [], label: [] });
  /** 只摘本组上一次加进去的覆盖物。clearOverlays() 会连同热力 Canvas 和其他图层一起摘掉，
   *  也会在每次数据变化时把用户正在看的地图清空重画。 */
  const replaceGroup = (instance: BMapMap, group: OverlayGroup, build: () => BMapOverlay[]) => {
    const previous = overlays.current[group];
    overlays.current[group] = [];
    for (const overlay of previous) instance.removeOverlay?.(overlay);
    overlays.current[group] = build();
  };
  // 每个小类一枚图标；选中态用实心变体突出。canvas 不可用时回退默认 Marker。
  const icons = useMemo(() => {
    if (!api) return null;
    const build = (filled: boolean) => Object.fromEntries(Object.entries(minorSymbols).map(([minor, symbol]) => {
      const major = minorMajor[minor] ?? 'life';
      return [minor, createDotIcon(api, { color: majorColors[major] ?? '#64748b', text: symbol, filled })];
    })) as Record<string, BMapIcon | undefined>;
    return { normal: build(false), selected: build(true) };
  }, [api]);
  useEffect(() => {
    if (!api || !container.current) return;
    let instance: BMapMap | undefined;
    const el = container.current;
    // Match BaiduMapView: cancel StrictMode's trial setup before the SDK starts async work.
    const frame = requestAnimationFrame(() => {
      try {
        instance = new api.Map(el);
        instance.centerAndZoom(new api.Point(initialCenter.current.lng, initialCenter.current.lat), 15);
        instance.enableScrollWheelZoom(true);
        instance.addEventListener('click', event => pick.current({ lng: +event.latlng.lng.toFixed(6), lat: +event.latlng.lat.toFixed(6) }));
        setMap(instance);
      } catch { setError(true); }
    });
    return () => { cancelAnimationFrame(frame); overlays.current = { vector: [], facility: [], label: [] }; instance?.destroy?.(); };
  }, [api]);
  // 覆盖物随地图实例建立与销毁；StrictMode 的二次挂载由 destroy 完全收尾。
  useEffect(() => {
    if (!api || !map || !heatSupported) return;
    const layer = createDensityOverlay(api);
    if (!layer) return;
    heat.current = layer;
    return () => { layer.destroy(); heat.current = null; };
  }, [api, map, heatSupported]);
  // 平移缩放结束后重新投影设施：聚合分格与视口一起变化，否则放大后标记会黏在原格子里。
  useEffect(() => {
    if (!map) return;
    let frame = 0;
    const onView = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => setViewTick(tick => tick + 1)); };
    for (const type of VIEW_EVENTS) map.addEventListener(type, onView);
    return () => { cancelAnimationFrame(frame); for (const type of VIEW_EVENTS) map.removeEventListener?.(type, onView); };
  }, [map]);

  // 数据与开关：设施、裁剪圈面和显隐互不影响矢量图层的重绘节奏。
  useEffect(() => {
    const layer = heat.current;
    if (!layer || !map) return;
    const band = result
      ? (result.timeBands?.find(item => item.minutes === minutes)?.geometry ?? (minutes === 15 ? result.geometry : null))
      : null;
    layer.setFacilities(heatPoints.map(item => ({ id: item.id, lng: item.location.lng, lat: item.location.lat })));
    layer.setBoundary(band);
    if (layers.heatmap) layer.attach(map); else layer.detach();
  }, [map, result, minutes, heatPoints, layers.heatmap]);

  // 矢量面：结果与图层开关变化时整组替换，只摘本组自己加的覆盖物。
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    try {
      replaceGroup(instance, 'vector', () => {
        const created: BMapOverlay[] = [];
        if (result) {
          if (layers.extent) created.push(...drawGeometry(instance, api, result.computationExtent, { strokeColor: '#64748b', fillOpacity: 0, strokeStyle: 'dashed', strokeWeight: 1 }));
          const band = result.timeBands?.find(item => item.minutes === minutes);
          const reachable = band ? band.geometry : minutes === 15 ? result.geometry : null;
          if (layers.reachable) {
            created.push(...(result.outlineOnly
              ? drawOutline(instance, api, result.displayGeometry === undefined ? reachable : result.displayGeometry)
              : drawGeometry(instance, api, reachable, { strokeColor: '#147d70', fillColor: '#2da990', fillOpacity: .28, strokeWeight: 2 })));
          }
          if (layers.serviceBlind) for (const region of Object.values(blindRegions)) created.push(...drawGeometry(instance, api, region as never, { strokeColor: '#4b5563', fillColor: '#6b7280', fillOpacity: .38, strokeWeight: 1 }));
          if (layers.unreachable) created.push(...drawGeometry(instance, api, result.unreachableRegion ?? null, { strokeColor: '#374151', fillColor: '#6b7280', fillOpacity: .28, strokeStyle: 'dashed' }));
          if (layers.unknown) created.push(...drawGeometry(instance, api, result.unknownRegion, { strokeColor: '#64748b', fillColor: '#64748b', fillOpacity: .24, strokeStyle: 'dashed' }));
          if (layers.uncertain) created.push(...drawGeometry(instance, api, result.uncertainRegion, { strokeColor: '#ca8a04', fillColor: '#facc15', fillOpacity: .15, strokeWeight: 1 }));
        }
        if (route.length > 1 && api.Polyline) {
          const line = new api.Polyline(route.map(p => new api.Point(...p)), { strokeColor: '#7c3aed', strokeWeight: 5 });
          instance.addOverlay(line); created.push(line);
        }
        if (resultCenter) {
          const analyzed = new api.Marker(new api.Point(resultCenter.lng, resultCenter.lat), { title: '已分析中心（与报告一致）' });
          instance.addOverlay(analyzed); created.push(analyzed);
        }
        if (!resultCenter || center.lng !== resultCenter.lng || center.lat !== resultCenter.lat) {
          const pending = new api.Marker(new api.Point(center.lng, center.lat), { title: '待分析选点（BD09LL）' });
          instance.addOverlay(pending); created.push(pending);
        }
        return created;
      });
    } catch { setError(true); }
  }, [api, map, center, result, resultCenter, layers, minutes, blindRegions, route]);

  // 设施标记：同格合并成一枚带数量的标记，全部设施都有归属，不按前 100 条截断。
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    try {
      replaceGroup(instance, 'facility', () => {
        const created: BMapOverlay[] = [];
        const aggregation = aggregateByCell(facilities.map(item => ({
          id: item.id, lng: item.location.lng, lat: item.location.lat, facility: item,
        })), {
          cellPx: FACILITY_CLUSTER_CELL_PX,
          project: (lng, lat) => instance.pointToOverlayPixel(new api.Point(lng, lat)),
        });
        for (const cell of aggregation.cells) {
          const primary = cell.items[0].facility;
          const icon = cell.count > 1
            ? createDotIcon(api, { color: majorColors[dominantMajor(cell.items.map(item => item.facility))] ?? '#64748b', text: String(cell.count), filled: true })
            : primary.id === selected ? icons?.selected[primary.category] : icons?.normal[primary.category];
          const marker = new api.Marker(new api.Point(cell.lng, cell.lat), {
            title: cell.count > 1 ? `${cell.count} 处设施聚合（点击查看其中一处）` : primary.name,
            ...(icon ? { icon } : {}),
          });
          marker.addEventListener('click', () => facilityClick.current?.(primary.id));
          instance.addOverlay(marker); created.push(marker);
        }
        return created;
      });
    } catch { setError(true); }
  }, [api, map, facilities, selected, icons, viewTick]);

  // 评估点标签独立成组：选中设施或平移地图都不会让它们跟着重建。
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    try {
      replaceGroup(instance, 'label', () => {
        const created: BMapOverlay[] = [];
        for (const point of assessments) {
          const state = point.categories.some(c => c.status === 'unknown') ? 'unknown' : point.categories.some(c => c.status === 'blind') ? 'blind' : 'covered';
          const labels = { covered: '有设施', blind: '查询内缺失', unknown: '无法判断' };
          const label = new api.Label(labels[state], { position: new api.Point(point.location.lng, point.location.lat) });
          label.setStyle({ color: '#fff', backgroundColor: { covered: '#147d70', blind: '#b54708', unknown: '#64748b' }[state], border: '0', padding: '3px' });
          instance.addOverlay(label); created.push(label);
        }
        return created;
      });
    } catch { setError(true); }
  }, [api, map, assessments]);

  // 视角只在"新结果"或"显式定位"（列表选中设施）时移动：勾选图层、刷新数据都不该把用户正在看的地方拽走。
  // 建图时 centerAndZoom 已经把视角放在初值上，因此初值不算一次"移动"。
  const pannedTo = useRef<string | null>(`${initialCenter.current.lng},${initialCenter.current.lat}`);
  const pannedSelection = useRef<string | null>(null);
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    const target = resultCenter ?? center;
    const key = `${target.lng},${target.lat}`;
    const selectionKey = result && selected ? selected : null;
    const movedToSelection = selectionKey !== null && selectionKey !== pannedSelection.current;
    pannedSelection.current = selectionKey;
    if (pannedTo.current === key && !movedToSelection) return;
    pannedTo.current = key;
    const focus = movedToSelection ? facilities.find(item => item.id === selectionKey) : null;
    const point = focus ? new api.Point(focus.location.lng, focus.location.lat) : new api.Point(target.lng, target.lat);
    try { instance.panTo(point); } catch { setError(true); }
  }, [api, map, center, resultCenter, result, selected, facilities]);

  const unavailable = error || mode === 'fallback';
  const failureMessage = failureReason === 'missing-key'
    ? '尚未配置浏览器地图密钥，请联系项目管理员完成地图配置。仍可输入坐标、执行分析和查看结果摘要。'
    : error
      ? '地图初始化或图层绘制失败，请刷新页面重试；持续失败时请联系项目管理员检查浏览器和地图兼容性。'
      : '百度地图脚本未能加载，请检查网络及浏览器地图密钥的权限和来源限制，修复后刷新页面。仍可输入坐标执行分析。';
  return <div className="api-map-shell">
    <div ref={container} className="api-map" data-testid="algorithm-map" aria-label="等时圈地图" />
    {(unavailable || mode === 'loading') && <div className="api-map-notice" role="status">
      <strong>{unavailable ? '地图不可用' : '正在加载百度地图'}</strong>
      <p>{unavailable ? failureMessage : '地图就绪后可点击选择分析中心。'}</p>
    </div>}
    {/* 左下角是一个纵向堆叠：说明行与热力图例共占用同一块位置，任何宽度下都不会互相压住。 */}
    <div className="api-map-left">
      <div className="api-map-caption">{result && ((result.timeBands?.find(item => item.minutes === minutes)?.geometry ?? (minutes === 15 ? result.geometry : null)) === null) && <><strong>{minutes} 分钟可达区域暂无有效数据</strong><br /></>}百度坐标 BD09LL · 点击地图选点
        {resultCenter && <><br />图层与报告中心：{resultCenter.lng.toFixed(6)}, {resultCenter.lat.toFixed(6)}</>}
      </div>
      {layers.heatmap && result && <div className="api-heat-legend" data-testid="heat-legend" aria-label="设施密度图例">
        <span className="api-legend-item"><i className="api-legend-ramp" style={{ background: densityLegendCss() }} />设施密度（{DENSITY_UNIT}）固定色标 0—{DENSITY_SCALE_MAX} · 单个设施中心 {SINGLE_FACILITY_PEAK.toFixed(2)}</span>
        <span className="api-legend-item"><i className="api-legend-ring" />核半径 {HEAT_KERNEL_RADIUS_M} 米 · 圈内设施等权去重 {heatPoints.length} 条</span>
        {!heatSupported && <span className="api-legend-note">当前地图脚本没有自定义覆盖物能力，热力层未启用。</span>}
        {heatSupported && heatPoints.length === 0 && <span className="api-legend-note">本次结果没有已确认在圈内的设施，未生成热力分布。</span>}
        {heatSupported && heatUndetermined > 0 && <span className="api-legend-note">另有 {heatUndetermined} 条设施尚未判定是否在圈内，未计入本层密度。</span>}
        {heatSupported && heatPoints.length > 0 && heatNotice && <span className="api-legend-note">{heatNotice}</span>}
        <span className="api-legend-note">显示的是检索分布密度，不等同于服务覆盖，低密度不能直接判为盲区。</span>
      </div>}
    </div>
    {facilities.length > 0 && <div className="api-map-legend" data-testid="map-legend" aria-label="地图图例">
      {Object.entries(displayGroupMeta).sort(([, left], [, right]) => left.order - right.order).map(([key, value]) => (
        <span key={key} className="api-legend-item"><i className="api-legend-dot" style={{ background: displayGroupColors[key] }} />{value.label}</span>
      ))}
      {facilities.length > 1 && <span className="api-legend-item">共 {facilities.length} 处设施，同格合并显示，数据不截断</span>}
      {route.length > 1 && <span className="api-legend-item"><i className="api-legend-line" />步行路线</span>}
    </div>}
  </div>;
}
