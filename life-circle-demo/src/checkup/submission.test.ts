import { describe, expect, it, vi } from 'vitest';
import { convertCoordinate } from './CoordinateInput';
import { validEssentialServices } from './validate';
import { essentialFixture } from './essentialFixture';

describe('submission contracts', () => {
  it('keeps historical results explicitly unassessed and validates three fixed partitions', () => {
    expect(validEssentialServices(undefined)).toBe(true);
    expect(validEssentialServices(essentialFixture())).toBe(true);
    const broken = essentialFixture(); broken.accessibility.categories[0].gapM2 = 0;
    expect(validEssentialServices(broken)).toBe(false);
    const duplicate = essentialFixture(); duplicate.accessibility.categories[1].category = 'market';
    expect(validEssentialServices(duplicate)).toBe(false);
  });
  it('applies only a valid official BD09 response', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({
      point: { lng: 121.5, lat: 31.3 }, coordinateSystem: 'bd09ll', provider: 'baidu_geoconv_v2' }) });
    expect(await convertCoordinate({ lng: 121, lat: 31 }, 'wgs84', fetcher)).toEqual({ lng: 121.5, lat: 31.3 });
    expect(JSON.parse(fetcher.mock.calls[0][1].body).sourceCoordinateSystem).toBe('wgs84');
    const refusal = vi.fn().mockResolvedValue({ ok: false, json: async () => ({ message: '权限不足' }) });
    await expect(convertCoordinate({ lng: 121, lat: 31 }, 'gcj02', refusal)).rejects.toThrow('权限不足');
    const invalid = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ point: { lng: 121, lat: 31 }, coordinateSystem: 'wgs84' }) });
    await expect(convertCoordinate({ lng: 121, lat: 31 }, 'gcj02', invalid)).rejects.toThrow('无效坐标');
  });
});
