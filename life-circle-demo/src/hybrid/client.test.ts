import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import { createHybridClient, validHybridResult, validHybridTask } from './client';

const fixture = () => JSON.parse(readFileSync(new URL('../../../backend/mocks/hybrid-partial.json', import.meta.url), 'utf8'));
const task = { schema_version: '1.0', responseType: 'task', taskId: 'one', status: 'running', businessStatus: null,
  stage: 'preparing', requests: 0, networkRequests: 0, budget: 40, elapsedSeconds: 0, dataSource: 'baidu_walking', error: null };

describe('dedicated Hybrid contract', () => {
  it('accepts legacy results without display geometry and validates new coordinates', () => {
    const r = fixture();
    delete r.isochrone.displayGeometry; delete r.algorithm.displayGeometry;
    expect(validHybridResult(r)).toBe(true);
    const bad = { ...r.isochrone.geometry, coordinateSystem: 'wgs84' };
    r.isochrone.displayGeometry = bad; r.algorithm.displayGeometry = bad;
    expect(validHybridResult(r)).toBe(false);
  });
  it('accepts the backend fixture and non-grid budgets', () => {
    expect(validHybridResult(fixture())).toBe(true);
    expect(validHybridTask(task)).toBe(true);
    expect(validHybridTask({ ...task, budget: 800 })).toBe(false);
  });
  it('rejects wrong coordinates, missing quality and out-of-domain config', () => {
    for (const edit of [
      (r: any) => { r.coordinateSystem = 'wgs84'; },
      (r: any) => { delete r.isochrone.quality; },
      (r: any) => { r.isochrone.config.analysis_half_width_m = 1600; },
      (r: any) => { r.isochrone.geometry.coordinates[0][0].pop(); },
      (r: any) => { r.isochrone.unknown_region = undefined; },
    ]) {
      const r = fixture(); edit(r); expect(validHybridResult(r)).toBe(false);
    }
  });
  it('uses Hybrid lookup/cancellation endpoints and surfaces backend codes', async () => {
    const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify(task), { status: 200 }));
    const client = createHybridClient('', fetcher);
    await client.byRequest('request one');
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/analysis/hybrid/by-request/request%20one');
    await client.cancelByRequest('request one');
    expect(fetcher.mock.calls[1][0]).toBe('/api/v1/analysis/hybrid/by-request/request%20one/cancel');
    expect(fetcher.mock.calls[1][1].method).toBe('POST');
    fetcher.mockResolvedValue(new Response(JSON.stringify({ code: 'hybrid_busy', message: 'busy' }), { status: 409 }));
    await expect(client.status('one')).rejects.toMatchObject({ code: 'hybrid_busy', status: 409 });
  });
  it('does not attach a response to a different task', async () => {
    const client = createHybridClient('', vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture()), { status: 200 })));
    await expect(client.result('other')).rejects.toMatchObject({ code: 'hybrid_task_mismatch' });
  });
});
