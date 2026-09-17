import { useEffect, useMemo, useRef, useState } from 'react';
import type { Center } from '../types';
import { useBaiduMap } from '../map/useBaiduMap';
import type { BMapIcon, BMapMap } from '../map/baiduMapTypes';
import { createDotIcon } from '../map/mapIcons';
import type { Isochrone } from './types';
import { drawGeometry, geometryForMinutes } from './geometry';
import type { Facility, AssessmentPoint } from '../api-contract';

export type Layers = { reachable: boolean; unreachable: boolean; unknown: boolean; uncertain: boolean; extent: boolean; serviceBlind: boolean };

/** 设施大类颜色（与图例、FacilityPanel 分组一致）；符号取小类首字。 */
const majorColors: Record<string, string> = { shopping: '#168875', medical: '#397ac6', education: '#c78b36' };
const majorNames: Record<string, string> = { shopping: '购物', medical: '医疗', education: '教育' };
const minorSymbols: Record<string, string> = { market: '菜', supermarket: '超', pharmacy: '药', hospital_pharmacy: '医', school: '学' };

export function ApiMap({ center, result, resultCenter, layers, onPick, minutes = 15, facilities = [], assessments = [], blindRegions = {}, route = [], selected, onFacility }: { center: Center; result?: Isochrone; resultCenter?: Center; layers: Layers; onPick: (center: Center) => void; minutes?: number; facilities?: Facility[]; assessments?: AssessmentPoint[]; blindRegions?: Record<string, unknown>; route?: [number, number][]; selected?: string | null; onFacility?: (id: string) => void }) {
  const { api, mode, failureReason } = useBaiduMap();
  const container = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<BMapMap | null>(null);
  const pick = useRef(onPick);
  pick.current = onPick;
  const initialCenter = useRef(center);
  initialCenter.current = center;
  const [error, setError] = useState(false);
  // 每个小类一枚图标；选中态用实心变体突出。canvas 不可用时回退默认 Marker。
  const icons = useMemo(() => {
    if (!api) return null;
    const build = (filled: boolean) => Object.fromEntries(Object.entries(minorSymbols).map(([minor, symbol]) => {
      const major = minor === 'market' || minor === 'supermarket' ? 'shopping'
        : minor === 'school' ? 'education' : 'medical';
      return [minor, createDotIcon(api, { color: majorColors[major], text: symbol, filled })];
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
    return () => { cancelAnimationFrame(frame); instance?.destroy?.(); };
  }, [api]);
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    try {
      instance.clearOverlays();
      instance.panTo(new api.Point(center.lng, center.lat));
      if (result) {
        if (layers.extent) drawGeometry(instance, api, result.computationExtent, { strokeColor: '#64748b', fillOpacity: 0, strokeStyle: 'dashed', strokeWeight: 1 });
        if (layers.reachable) drawGeometry(instance, api, geometryForMinutes(result, minutes), { strokeColor: '#147d70', fillColor: '#2da990', fillOpacity: .28, strokeWeight: 2 });
        if (layers.serviceBlind) Object.values(blindRegions).forEach(region => drawGeometry(instance, api, region as never, { strokeColor: '#4b5563', fillColor: '#6b7280', fillOpacity: .38, strokeWeight: 1 }));
        if (layers.unreachable) drawGeometry(instance, api, result.unreachableRegion ?? null, { strokeColor: '#374151', fillColor: '#6b7280', fillOpacity: .28, strokeStyle: 'dashed' });
        if (layers.unknown) drawGeometry(instance, api, result.unknownRegion, { strokeColor: '#64748b', fillColor: '#64748b', fillOpacity: .24, strokeStyle: 'dashed' });
        if (layers.uncertain) drawGeometry(instance, api, result.uncertainRegion, { strokeColor: '#ca8a04', fillColor: '#facc15', fillOpacity: .15, strokeWeight: 1 });
      }
      for (const facility of facilities.slice(0,100)) {
        const icon = facility.id === selected ? icons?.selected[facility.category] : icons?.normal[facility.category];
        const marker = new api.Marker(new api.Point(facility.location.lng,facility.location.lat), {title:facility.name, ...(icon ? { icon } : {})});
        marker.addEventListener('click', ()=>onFacility?.(facility.id));
        instance.addOverlay(marker);
      }
      for (const p of assessments) {
        const state = p.categories.some(c=>c.status==='unknown') ? 'unknown' : p.categories.some(c=>c.status==='blind') ? 'blind' : 'covered';
        const labels={covered:'有设施',blind:'查询内缺失',unknown:'无法判断'};
        const label = new api.Label(labels[state],{position:new api.Point(p.location.lng,p.location.lat)});
        label.setStyle({color:'#fff',backgroundColor:{covered:'#147d70',blind:'#b54708',unknown:'#64748b'}[state],border:'0',padding:'3px'});
        instance.addOverlay(label);
      }
      if (route.length>1 && api.Polyline) instance.addOverlay(new api.Polyline(route.map(p=>new api.Point(...p)),{strokeColor:'#7c3aed',strokeWeight:5}));
      if (resultCenter) instance.addOverlay(new api.Marker(new api.Point(resultCenter.lng, resultCenter.lat), { title: '已分析中心（与报告一致）' }));
      if (!resultCenter || center.lng !== resultCenter.lng || center.lat !== resultCenter.lat) {
        instance.addOverlay(new api.Marker(new api.Point(center.lng, center.lat), { title: '待分析选点（BD09LL）' }));
      }
    } catch { setError(true); }
  }, [api, map, center, result, resultCenter, layers, minutes, facilities, assessments, blindRegions, route, selected, icons, onFacility]);
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
    <div className="api-map-caption">{result && geometryForMinutes(result, minutes) === null && <><strong>{minutes} 分钟可达区域暂无有效数据</strong><br /></>}百度坐标 BD09LL · 点击地图选点
      {resultCenter && <><br />图层与报告中心：{resultCenter.lng.toFixed(6)}, {resultCenter.lat.toFixed(6)}</>}
    </div>
    {facilities.length > 0 && <div className="api-map-legend" data-testid="map-legend" aria-label="地图图例">
      <span className="api-legend-item"><i className="api-legend-dot" style={{ background: majorColors.shopping }} />{majorNames.shopping}（菜/超）</span>
      <span className="api-legend-item"><i className="api-legend-dot" style={{ background: majorColors.medical }} />{majorNames.medical}（药/医）</span>
      <span className="api-legend-item"><i className="api-legend-dot" style={{ background: majorColors.education }} />{majorNames.education}（学）</span>
      {route.length > 1 && <span className="api-legend-item"><i className="api-legend-line" />步行路线</span>}
    </div>}
  </div>;
}
