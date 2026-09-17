import { test, expect, type Page } from '@playwright/test';

type LocationFixture = {
  geolocation?: 'ok' | 'denied' | 'timeout';
  accuracy?: number;
  nearbyEmpty?: boolean;
  places?: { title: string; address?: string; uid?: string; lng: number; lat: number }[];
  farPlaces?: { title: string; address?: string; uid?: string; lng: number; lat: number }[];
  geocode?: { lng: number; lat: number } | null;
};

async function mockMap(page: Page, location: LocationFixture = {}) {
  await page.addInitScript((fixture: LocationFixture) => {
    const storage = window as unknown as { __polygons: { points: string[]; options: { fillColor?: string } }[] };
    storage.__polygons = [];
    class Overlay { addEventListener() {} removeEventListener() {} }
    class Point { constructor(public lng: number, public lat: number) {} }
    class Size { constructor(public width: number, public height: number) {} }
    class Icon { constructor(public url: string, public size: Size, public options: { anchor?: Size }) {} }
    class Map {
      private center = new Point(116.404, 39.915);
      private svg: SVGSVGElement;
      constructor(private container: HTMLElement) {
        container.dataset.sdk = 'offline';
        this.svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        this.svg.setAttribute('viewBox', '0 0 1000 900');
        this.svg.setAttribute('width', '100%'); this.svg.setAttribute('height', '100%');
        container.append(this.svg);
      }
      centerAndZoom(center: Point) { this.center = center; }
      panTo(center: Point) { this.center = center; }
      enableScrollWheelZoom() {}
      addOverlay(overlay: Polygon) {
        if (!overlay.points) return;
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', overlay.points.map(ring => ring.split(';').map((point, index) => {
          const [lng, lat] = point.split(',').map(Number);
          return `${index ? 'L' : 'M'}${500 + (lng - this.center.lng) * 25000 * Math.cos(this.center.lat * Math.PI / 180)},${450 - (lat - this.center.lat) * 25000}`;
        }).join(' ') + 'Z').join(' '));
        path.setAttribute('fill-rule', 'evenodd');
        path.setAttribute('fill', overlay.options.fillColor || 'none');
        path.setAttribute('fill-opacity', String(overlay.options.fillOpacity ?? .2));
        path.setAttribute('stroke', overlay.options.strokeColor || '#64748b');
        this.svg.append(path);
      }
      clearOverlays() {
        storage.__polygons = []; this.svg.replaceChildren();
        const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        label.setAttribute('x', '24'); label.setAttribute('y', '36'); label.textContent = '离线 SDK 替身 · 不含真实底图';
        this.svg.append(label);
      }
      destroy() { this.svg.remove(); }
      addEventListener() {}
    }
    class Polygon extends Overlay {
      constructor(public points: string[], public options: { fillColor?: string; fillOpacity?: number; strokeColor?: string }) { super(); storage.__polygons.push({ points, options }); }
    }
    class Geolocation {
      private status = 0;
      getCurrentPosition(callback: (result: unknown) => void) {
        const mode = fixture.geolocation ?? 'ok';
        if (mode === 'denied') { this.status = 6; setTimeout(() => callback(null), 0); return; }
        if (mode === 'timeout') { this.status = 8; setTimeout(() => callback(null), 0); return; }
        setTimeout(() => callback({
          point: { lng: 116.418, lat: 39.921 }, accuracy: fixture.accuracy ?? 30,
          address: { province: '北京市', city: '北京市', district: '东城区', street: '测试街', streetNumber: '1号' },
        }), 0);
      }
      getStatus() { return this.status; }
    }
    class LocalSearch {
      constructor(public location: unknown, public options: { onSearchComplete?: (results: unknown) => void; pageCapacity?: number }) {}
      private respond(places: NonNullable<LocationFixture['places']>) {
        const result = {
          getPoi: (index: number) => places[index]
            ? { ...places[index], point: { lng: places[index].lng, lat: places[index].lat } }
            : undefined,
          getCurrentNumPois: () => places.length,
          getNumPois: () => places.length,
        };
        setTimeout(() => this.options.onSearchComplete?.(result), 0);
      }
      searchNearby() { this.respond(fixture.nearbyEmpty ? [] : fixture.places ?? []); }
      searchInBounds() { this.respond(fixture.farPlaces ?? []); }
      search() {}
      clearResults() {}
    }
    class Bounds { constructor(public sw: unknown, public ne: unknown) {} }
    class Geocoder {
      getPoint(_address: string, callback: (point: unknown) => void) {
        setTimeout(() => callback(fixture.geocode ?? null), 0);
      }
    }
    class Label extends Overlay { setStyle() {} }
    class Polyline extends Overlay {}
    Object.assign(window, { BMapGL: { Map, Point, Size, Icon, Polygon, Marker: Overlay, Label, Polyline, Geolocation, LocalSearch, Bounds, Geocoder } });
  }, location);
}

test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => {
    const host = new URL(route.request().url()).hostname;
    return host === '127.0.0.1' || host === 'localhost' ? route.continue() : route.abort();
  });
});

async function analyze(page: Page, lng = '116.404') {
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill(lng);
  const response = page.waitForResponse(r => /\/api\/analyses\/[^/]+\/result$/.test(r.url()) && r.status() === 200);
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const result = await (await response).json();
  await expect(page.getByText('分析完成', { exact: true })).toBeVisible();
  if (result.isochrone.quality !== 'insufficient' && result.isochrone.geometry !== null) {
    await expect(page.getByTestId('analysis-report')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('analysis-report')).not.toBeVisible();
  }
  return result;
}

test('real HTTP algorithm chain, direct BD09 polygons and responsive layout', async ({ page }, info) => {
  await mockMap(page);
  await page.goto('/');
  await expect(page.getByText('设施统计尚未接入', { exact: true })).toBeVisible();
  const result = await analyze(page);
  expect(result.isochrone.statistics.requests).toBeLessThanOrEqual(400);
  expect(result.isochrone.statistics.network_requests).toBe(0);
  await expect(page.getByText('合成数据 · 离线验收', { exact: true })).toBeVisible();
  const overlays = await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#2da990'));
  expect(overlays.map((p: any) => p.points)).toEqual(result.isochrone.geometry.coordinates.map((p: number[][][]) => p.map(r => r.map(x => x.join(',')).join(';'))));
  await page.screenshot({ path: info.outputPath('desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: info.outputPath('mobile.png'), fullPage: true });
});

test('holes and multiple components survive transport and map conversion', async ({ page }) => {
  await mockMap(page);
  await page.goto('/');
  const hole = await analyze(page, '116.405');
  expect(hole.isochrone.geometry.coordinates.some((polygon: unknown[]) => polygon.length > 1)).toBe(true);
  const overlays = await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#2da990'));
  expect(overlays.some((p: any) => p.points.length > 1)).toBe(true);
  const components = await analyze(page, '116.406');
  expect(components.isochrone.geometry.coordinates.length).toBeGreaterThan(1);
});

test('unknown evidence and supported empty geometry have different UI', async ({ page }) => {
  await mockMap(page);
  await page.goto('/');
  const unknown = await analyze(page, '116.407');
  expect(unknown.isochrone.geometry).toBeNull();
  await expect(page.getByText('证据不足，无法确定可达区域', { exact: true })).toBeVisible();
  const empty = await analyze(page, '116.408');
  expect(empty.isochrone.geometry.coordinates).toEqual([]);
  await expect(page.getByText('有效证据范围内，可达区域为空', { exact: true })).toBeVisible();
});

test('local unknown remains a separate layer', async ({ page }) => {
  await mockMap(page);
  await page.goto('/');
  const result = await analyze(page, '116.410');
  expect(result.isochrone.unknownRegion.coordinates.length).toBeGreaterThan(0);
  const unknown = await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#64748b'));
  expect(unknown.length).toBe(result.isochrone.unknownRegion.coordinates.length);
  const unreachableBefore = await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#6b7280'));
  await page.getByRole('checkbox', { name: '未核验区域（灰色）' }).uncheck();
  expect(await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#6b7280'))).toEqual(unreachableBefore);
  await expect(page.getByRole('checkbox', { name: '已知不可达区域', exact: true })).toBeChecked();
  expect(await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#64748b').length)).toBe(0);
});

test('cancels the server task and clears old state when center changes', async ({ page, request }) => {
  await mockMap(page);
  await page.goto('/');
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.409');
  const created = page.waitForResponse(r => r.url().endsWith('/api/analyses') && r.status() === 202);
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const id = (await (await created).json()).taskId;
  await page.getByRole('button', { name: '取消任务', exact: true }).click();
  await expect(page.getByText('任务已取消', { exact: true })).toBeVisible();
  const status = await (await request.get(`http://127.0.0.1:8018/api/analyses/${id}`)).json();
  expect(status.status).toBe('cancelled');
  expect(status.requests).toBeLessThan(status.budget);
  // Browser scheduling changes the count before the click; cancellation must stop growth.
  await page.waitForTimeout(500);
  const settled = await (await request.get(`http://127.0.0.1:8018/api/analyses/${id}`)).json();
  expect(settled.status).toBe('cancelled');
  expect(settled.requests).toBe(status.requests);
  expect((await request.get(`http://127.0.0.1:8018/api/analyses/${id}/result`)).status()).toBe(409);
  await page.getByRole('spinbutton', { name: '纬度', exact: true }).fill('39.916');
  await expect(page.getByText('任务已取消', { exact: true })).not.toBeVisible();
});

test('SDK failure keeps coordinate analysis and summary usable without demo fallback', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('地图不可用', { exact: true })).toBeVisible();
  await analyze(page);
  await expect(page.getByRole('region', { name: '分析结果', exact: true }).getByText(/已重建 \d+ 个可达分量/)).toBeVisible();
  await expect(page.getByText('演示数据', { exact: true })).not.toBeVisible();
});

test('network failure is explicit and can resume the same analysis request', async ({ page }) => {
  await mockMap(page);
  await page.route('**/api/analyses', route => route.abort(), { times: 1 });
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('无法连接分析服务或请求超时，请检查网络和服务地址后重试', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await expect(page.getByText('分析完成', { exact: true })).toBeVisible();
  await expect(page.getByTestId('analysis-report')).toBeVisible();
});

test('reports retain their original conditions after edits, unavailable results and failed retries', async ({ page }) => {
  await mockMap(page);
  await page.goto('/');
  await analyze(page);
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  const report = page.getByTestId('analysis-report');
  await expect(report).toContainText('116.404000, 39.915000');
  await expect(report).toContainText('合成时间场（非真实社区）');
  await expect(page.getByTestId('analysis-facility-stats').getByRole('cell', { name: '无法确定', exact: true })).toHaveCount(3);
  await expect(report).toContainText('设施盲区数量：无法确定');
  await page.keyboard.press('Escape');
  await analyze(page, '116.407');
  await expect(page.getByText('本次步行证据不足，未生成新的体检报告。', { exact: true })).toBeVisible();
  await expect(report).not.toBeVisible();
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(report).toContainText('116.404000, 39.915000');
  await expect(report).toContainText('分析条件已修改');
  await expect(report).toContainText('最近一次分析未成功');
  await page.keyboard.press('Escape');
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.406');
  await page.route('**/api/analyses', route => route.fulfill({ status: 503, body: '{}' }), { times: 1 });
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('分析服务当前不可用，请联系管理员检查步行服务配置', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(report).toContainText('116.404000, 39.915000');
  await expect(report).toContainText('最近一次分析未成功');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await expect(report).toBeVisible();
  await expect(report).toContainText('116.406000, 39.915000');
  await expect(report).not.toContainText('分析条件已修改');
  await expect(report).not.toContainText('最近一次分析未成功');
});

test('lost create response can be cancelled by request key after editing center', async ({ page, request }) => {
  await mockMap(page);
  let taskId = '';
  await page.route('**/api/analyses', async route => {
    const accepted = await route.fetch();
    taskId = (await accepted.json()).taskId;
    await route.abort(); // Server accepted the slow job, but the browser never received its ID.
  }, { times: 1 });
  await page.goto('/');
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.409');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('无法连接分析服务或请求超时，请检查网络和服务地址后重试', { exact: true })).toBeVisible();
  const cancelled = page.waitForResponse(r => r.url().includes('/by-request/') && r.status() === 202);
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.404');
  await cancelled;
  await expect.poll(async () => (await (await request.get(`http://127.0.0.1:8018/api/analyses/${taskId}`)).json()).status).toBe('cancelled');
  await analyze(page);
});

test('malformed successful result shows a format error instead of crashing the page', async ({ page }) => {
  await mockMap(page);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/analyses/*/result', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('后端返回格式异常，请检查服务版本', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('device location fills the center and reports accuracy and address', async ({ page }) => {
  await mockMap(page, { geolocation: 'ok' });
  await page.goto('/');
  await page.getByRole('button', { name: '获取当前位置', exact: true }).click();
  await expect(page.getByText(/已定位：北京市东城区测试街1号 · 定位精度约 30 米/)).toBeVisible();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue(/116\.418/);
  await expect(page.getByRole('spinbutton', { name: '纬度', exact: true })).toHaveValue(/39\.921/);
});

test('coarse device location warns for map verification', async ({ page }) => {
  await mockMap(page, { geolocation: 'ok', accuracy: 800 });
  await page.goto('/');
  await page.getByRole('button', { name: '获取当前位置', exact: true }).click();
  await expect(page.getByText(/定位可能偏差较大，请在地图上核对/)).toBeVisible();
});

test('denied device location keeps manual coordinates and explains how to retry', async ({ page }) => {
  await mockMap(page, { geolocation: 'denied' });
  await page.goto('/');
  await page.getByRole('button', { name: '获取当前位置', exact: true }).click();
  await expect(page.getByText('定位权限被拒绝，请在浏览器设置中允许定位后重试', { exact: true })).toBeVisible();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue(/116\.404/);
});

test('POI search selection becomes the analysis center', async ({ page }) => {
  await mockMap(page, { places: [{ title: '测试公园', address: '测试路1号', uid: 'poi-1', lng: 116.5, lat: 39.95 }] });
  await page.goto('/');
  await page.getByRole('textbox', { name: '搜索地点' }).fill('公园');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await page.getByRole('button', { name: /测试公园/ }).click();
  await expect(page.getByText(/已选择：测试公园 · 测试路1号/)).toBeVisible();
  const created = page.waitForRequest(r => r.url().endsWith('/api/analyses') && r.method() === 'POST');
  const result = page.waitForResponse(r => /\/api\/analyses\/[^/]+\/result$/.test(r.url()) && r.status() === 200);
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  expect((await created).postDataJSON().center).toEqual({ lng: 116.5, lat: 39.95 });
  await result;
});

test('empty POI search result shows a retry hint', async ({ page }) => {
  await mockMap(page, { places: [] });
  await page.goto('/');
  await page.getByRole('textbox', { name: '搜索地点' }).fill('不存在的地方');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.getByText('未找到相关地点，请尝试其他关键词', { exact: true })).toBeVisible();
});

test('POI search falls back to far options when nothing is nearby', async ({ page }) => {
  await mockMap(page, {
    nearbyEmpty: true,
    farPlaces: [
      { title: '远郊公园', address: '远郊路9号', uid: 'poi-far', lng: 117.2, lat: 40.3 },
      { title: '远郊花园', address: '远郊路10号', uid: 'poi-loose', lng: 117.3, lat: 40.3 },
    ],
  });
  await page.goto('/');
  await page.getByRole('textbox', { name: '搜索地点' }).fill('公园');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.getByText('较远结果（超过 5 公里）', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /远郊花园/ })).toHaveCount(0);
  await page.getByRole('button', { name: /远郊公园/ }).click();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue(/117\.2/);
});

test('POI search lists nearby results first and keeps far options', async ({ page }) => {
  await mockMap(page, {
    places: [{ title: '近处公园', address: '近处路1号', uid: 'poi-near', lng: 116.41, lat: 39.92 }],
    farPlaces: [{ title: '远郊公园', address: '远郊路9号', uid: 'poi-far', lng: 117.2, lat: 40.3 }],
  });
  await page.goto('/');
  await page.getByRole('textbox', { name: '搜索地点' }).fill('公园');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.getByText('附近结果（5 公里内）', { exact: true })).toBeVisible();
  await expect(page.getByText('较远结果（超过 5 公里）', { exact: true })).toBeVisible();
  const options = page.getByRole('button', { name: /公园/ });
  await expect(options).toHaveCount(2);
  await options.first().click();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue(/116\.41/);
});

test('POI search resolves a nationwide administrative name through address fallback', async ({ page }) => {
  await mockMap(page, { geocode: { lng: 120.43, lat: 27.52 } });
  await page.goto('/');
  await page.getByRole('textbox', { name: '搜索地点' }).fill('苍南县');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.getByText('较远结果（超过 5 公里）', { exact: true })).toBeVisible();
  await expect(page.getByText('地址定位', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /苍南县/ }).click();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue(/120\.43/);
});

test('facility report, category filtering, route and time layers share one analysis', async ({ page }, info) => {
  await mockMap(page);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const location = { lng: 116.405, lat: 39.915 };
  const savedRoute = { poiEvidence: { version: '1.0', facilityId: 'pharmacy-fixture', status: 'verified_reachable',
    reason: null, duration: 500, observedDuration: 500, endpointVerified: true,
    requestOrigin: [116.404,39.915], destination: [location.lng,location.lat],
    routeOrigin: [116.404,39.915], routeDestination: [location.lng,location.lat], originOffsetM: 0, destinationOffsetM: 0 }, distance_m: 600, duration_s: 500, endpoint_verified: true, reason: null, path: [[116.404, 39.915], [116.405, 39.915]] };
  await page.route('**/api/analyses/*/result', async route => {
    const response = await route.fetch();
    const data = await response.json();
    data.facilitiesStatus = 'partial';
    data.data.facilities = [{ id: 'pharmacy-fixture', name: '离线测试药房', category: 'pharmacy', minor_category: 'pharmacy', major_category: 'medical', location, in_circle: true, poiEvidence: savedRoute.poiEvidence }];
    data.data.report = '离线业务样例：1处设施，未知不当盲区。';
    data.facilityAnalysis = {
      status: 'partial',
      queries: [{ category: 'pharmacy', query: '药店', status: 'truncated', pages: 2, returned: 1, excluded: 0, invalid: 0, total: 150, reason: 'page_limit' }],
      assessments: [{ location, duration_s: 500, categories: [
        { category: 'shopping', status: 'unknown', facility_id: null, distance_m: null, reason: 'incomplete' },
        { category: 'medical', status: 'covered', facility_id: 'pharmacy-fixture', distance_m: 600, reason: 'walking' },
        { category: 'education', status: 'unknown', facility_id: null, distance_m: null, reason: 'incomplete' },
      ] }],
      candidate_points: 2, assessed_points: 1, unassessed_points: 1, network_requests: 0, elapsed_seconds: 0, search_radius_m: 3500,
      routes: { 'pharmacy-fixture': savedRoute },
      serviceBlindRegions: {
        shopping: { type: 'MultiPolygon', coordinateSystem: 'bd09ll', coordinates: [] },
        medical: { type: 'MultiPolygon', coordinateSystem: 'bd09ll', coordinates: [] },
        education: { type: 'MultiPolygon', coordinateSystem: 'bd09ll', coordinates: [] },
      },
      warnings: ['离线样例，未测点不计入盲区。'],
    };
    await route.fulfill({ response, json: data });
  });
  await page.route('**/api/analyses/*/routes/*', route => route.fulfill({ json: savedRoute }));
  await page.goto('/');
  await analyze(page);
  await expect(page.getByText('设施与基础报告', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /离线测试药房/ }).click();
  await page.getByRole('button', { name: '查看中心到设施的步行路线' }).click();
  await expect(page.getByText('600 米 · 500 秒 · 严格核验：15分钟内可达')).toBeVisible();
  await expect(page.getByTestId('route-poi-evidence')).toContainText('严格核验：15分钟内可达');
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(page.getByTestId('strict-poi-counts')).toContainText('15分钟内可达 1');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('analysis-report')).not.toBeVisible();
  await page.getByRole('combobox', { name: '步行时间层' }).click();
  await page.getByTitle('5 分钟', { exact: true }).click();
  await expect(page.getByText('离线业务样例：1处设施，未知不当盲区。', { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('facilities-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByText('设施与基础报告', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('facilities-mobile.png'), fullPage: true });
  expect(errors).toEqual([]);
});
