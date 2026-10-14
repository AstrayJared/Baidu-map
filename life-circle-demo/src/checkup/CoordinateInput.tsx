import { useEffect, useRef, useState } from 'react';
import { Alert, Button, InputNumber, Select } from 'antd';
import type { Center } from '../types';
import type { CoordinateRequest } from './contract';

export type CoordinateSource = CoordinateRequest['sourceCoordinateSystem'];
export async function convertCoordinate(point: Center, source: CoordinateSource, fetcher = fetch): Promise<Center> {
  const base = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, '') || '';
  const response = await fetcher(`${base}/api/v2/coordinates/convert`, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ point, sourceCoordinateSystem: source }) });
  const body = await response.json();
  if (!response.ok) throw new Error(body.message || '官方坐标转换失败');
  if (body.coordinateSystem !== 'bd09ll' || !body.point || !Number.isFinite(body.point.lng)
    || !Number.isFinite(body.point.lat) || Math.abs(body.point.lng) > 180 || Math.abs(body.point.lat) >= 85) {
    throw new Error('官方坐标转换返回无效坐标');
  }
  return body.point;
}

export default function CoordinateInput({ center, onPick }: { center: Center; onPick: (point: Center) => void }) {
  const [source, setSource] = useState<CoordinateSource>('bd09ll');
  const [lng, setLng] = useState<number | null>(center.lng);
  const [lat, setLat] = useState<number | null>(center.lat);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  useEffect(() => { generation.current++; setBusy(false); }, [center.lng, center.lat]);
  useEffect(() => () => { generation.current++; }, []);
  const invalidate = () => { generation.current++; setBusy(false); setError(null); };
  async function apply() {
    if (lng === null || lat === null) return;
    const current = ++generation.current;
    setBusy(true); setError(null);
    try {
      const point = await convertCoordinate(source === 'bd09ll' ? center : { lng, lat }, source);
      if (generation.current === current) onPick(point);
    } catch (e) { if (generation.current === current) setError(e instanceof Error ? e.message : '转换失败'); }
    finally { if (generation.current === current) setBusy(false); }
  }
  return <div data-testid="coordinate-conversion">
    <Select aria-label="来源坐标系" value={source} onChange={value => { invalidate(); setSource(value); }}
      options={[{ value: 'bd09ll', label: '百度 BD09LL（上方坐标）' }, { value: 'gcj02', label: 'GCJ02（高德/腾讯）' }, { value: 'wgs84', label: 'WGS84（GPS）' }]} />
    {source !== 'bd09ll' && <div className="wb-coords">
      <InputNumber aria-label="源经度" value={lng} min={-180} max={180} onChange={v => { invalidate(); setLng(v); }} />
      <InputNumber aria-label="源纬度" value={lat} min={-84.999999} max={84.999999} onChange={v => { invalidate(); setLat(v); }} />
    </div>}
    <Button aria-label="应用坐标" loading={busy} onClick={() => void apply()} disabled={lng === null || lat === null}>应用坐标</Button>
    {source !== 'bd09ll' && <p className="wb-hint">转换成功后更新地图中心；上方始终显示地图使用的 BD09LL 坐标。</p>}
    {error && <Alert type="error" title={error} />}
  </div>;
}
