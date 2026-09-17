import { test, expect, type Page } from '@playwright/test';
import { resultFixture } from '../src/analysis/testFixtures';

/** Contract-only browser tests. All external requests are blocked; no backend/AK is used. */
async function setup(page: Page, options: { failOnce?: boolean; unavailable?: boolean; mismatch?: boolean;
  createGate?: Promise<void>; resultGate?: Promise<void>; running?: boolean; createError?: number; statusError?: number; failed?: boolean;
  withFacilities?: boolean } = {}) {
  let submitted: { center: { lng: number; lat: number }; budget: number };
  let count = 0;
  await page.addInitScript(() => {
    const audit = { creations: 0, active: 0, paths: [] as string[][],
      polylines: [] as { lng: number; lat: number }[][],
      markers: [] as Marker[], click: undefined as undefined | ((e: unknown) => void) };
    let iconSeq = 0;
    class Overlay {
      handlers: Record<string, () => void> = {};
      addEventListener(type: string, handler: () => void) { this.handlers[type] = handler; }
      removeEventListener() {}
    }
    class Point { constructor(public lng: number, public lat: number) {} }
    class Size { constructor(public width: number, public height: number) {} }
    class Icon { seq = ++iconSeq; constructor(public url: string, public size: Size, public options: { anchor?: Size }) {} }
    class Marker extends Overlay { constructor(public point: Point, public options: { title: string; icon?: Icon }) { super(); } }
    class Polygon extends Overlay { constructor(public rings: string[]) { super(); } }
    class Label extends Overlay { setStyle() {} }
    class Polyline extends Overlay { constructor(public points: Point[]) { super(); audit.polylines.push(points.map(p => ({ lng: p.lng, lat: p.lat }))); } }
    class Map {
      constructor(el: HTMLElement) {
        audit.creations++; audit.active++;
        el.addEventListener('click', () => audit.click?.({ latlng: { lng: 116.405, lat: 39.916 } }));
      }
      centerAndZoom() {} panTo() {} enableScrollWheelZoom() {}
      addEventListener(_event: string, handler: (e: unknown) => void) { audit.click = handler; }
      addOverlay(overlay: Polygon | Marker) {
        if (overlay instanceof Polygon) audit.paths.push(overlay.rings);
        if (overlay instanceof Marker) audit.markers.push(overlay);
      }
      clearOverlays() { audit.paths = []; audit.markers = []; audit.polylines = []; }
      destroy() { audit.active--; }
    }
    Object.assign(window, { BMapGL: { Map, Point, Size, Icon, Polygon, Marker, Label, Polyline }, __mapAudit: audit });
  });
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.hostname !== '127.0.0.1') return route.abort();
    if (!url.pathname.startsWith('/api/analyses')) return route.continue();
    if (url.pathname === '/api/analyses') {
      submitted = route.request().postDataJSON(); count++;
      await options.createGate;
      if (options.createError) return route.fulfill({ status: options.createError, body: 'private upstream details' });
      if (options.failOnce) { options.failOnce = false; return route.fulfill({ status: 503, json: {} }); }
    }
    const taskId = `task-${count}`;
    if (url.pathname.endsWith('/result')) {
      await options.resultGate;
      const result = resultFixture();
      result.taskId = taskId; result.center = { ...submitted.center };
      result.isochrone.config = { ...result.isochrone.config, budget: submitted.budget,
        origin: [submitted.center.lng, submitted.center.lat] };
      result.isochrone.quality = 'partial';
      if (options.unavailable) { result.isochrone.geometry = null; result.isochrone.quality = 'insufficient'; }
      if (options.mismatch) {
        result.center.lng = 120;
        result.isochrone.config.origin[0] = 120;
      }
      if (options.withFacilities) {
        result.facilitiesStatus = 'partial';
        result.data = { ...result.data, report: '离线契约样例：1 处设施，未知不判为盲区。',
          facilities: [{ id: 'pharmacy-fixture', name: '离线测试药房', category: 'pharmacy', minor_category: 'pharmacy',
            major_category: 'medical', location: { ...submitted.center }, in_circle: true }] };
        result.facilityAnalysis = {
          status: 'partial',
          queries: [{ category: 'pharmacy', query: '药店', status: 'truncated', pages: 2, returned: 1, excluded: 0, invalid: 0, total: 150, reason: 'page_limit' }],
          assessments: [{ location: { ...submitted.center }, duration_s: 500, categories: [
            { category: 'shopping', status: 'unknown', facility_id: null, distance_m: null, reason: 'incomplete' },
            { category: 'medical', status: 'covered', facility_id: 'pharmacy-fixture', distance_m: 600, reason: 'walking' },
            { category: 'education', status: 'unknown', facility_id: null, distance_m: null, reason: 'incomplete' },
          ] }],
          candidate_points: 2, assessed_points: 1, unassessed_points: 1, network_requests: 0, elapsed_seconds: 0, search_radius_m: 3500,
          routes: {},
          serviceBlindRegions: {},
          warnings: ['离线样例，未测点不计入盲区。'],
        };
      }
      return route.fulfill({ json: result });
    }
    if (route.request().method() === 'GET' && options.statusError) return route.fulfill({ status: options.statusError, json: {} });
    const status = url.pathname.endsWith('/cancel') ? 'cancelled' : options.failed ? 'failed' : options.running ? 'running' : 'completed';
    return route.fulfill({ status: route.request().method() === 'POST' ? 202 : 200,
      json: { schema_version: '1.0', responseType: 'task', businessStatus: status === 'completed' ? 'partial' : status === 'failed' ? 'failed' : null,
        taskId, status, stage: status === 'running' ? 'refining' : status, requests: 200, networkRequests: 0,
        budget: submitted.budget, elapsedSeconds: 1, dataSource: 'synthetic', error: null } });
  });
  return { creations: () => count };
}

test('validated partial result drives map and automatic report, preserving holes and unknown counts', async ({ page }) => {
  await setup(page);
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const report = page.getByTestId('analysis-report');
  await expect(report).toBeVisible();
  await expect(report).toContainText('部分体检结果');
  await expect(report).toContainText('证据质量：部分结果');
  await expect(page.getByTestId('analysis-facility-stats').getByRole('cell', { name: '无法确定', exact: true })).toHaveCount(3);
  // No facilities in this scenario, so the map legend must stay hidden.
  await expect(page.getByTestId('map-legend')).not.toBeVisible();
  const audit = await page.evaluate(() => (window as any).__mapAudit);
  expect(audit.active).toBe(1);
  expect(audit.creations).toBe(1);
  expect(audit.paths).toEqual(resultFixture().isochrone.geometry!.coordinates.map(p => p.map(r => r.map(x => x.join(',')).join(';'))));
  await page.keyboard.press('Escape');
  await page.getByRole('checkbox', { name: '可达区域', exact: true }).uncheck();
  expect(await page.evaluate(() => (window as any).__mapAudit.paths.length)).toBe(0);
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(report).toContainText('已重建 2 个可达分量');
  expect(errors).toEqual([]);
});

test('map selection and failed retry retain the old report until a valid replacement arrives', async ({ page }) => {
  const options = { failOnce: false };
  await setup(page, options);
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const report = page.getByTestId('analysis-report');
  await expect(report).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByTestId('algorithm-map').click();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue('116.405000');
  options.failOnce = true;
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('分析服务当前不可用，请联系管理员检查步行服务配置', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(report).toContainText('116.404000, 39.915000');
  await expect(report).toContainText('分析条件已修改');
  await expect(report).toContainText('最近一次分析未成功');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await expect(report).toBeVisible();
  await expect(report).toContainText('116.405000, 39.916000');
  await expect(report).not.toContainText('分析条件已修改');
  await expect(report).not.toContainText('最近一次分析未成功');
});

test('insufficient evidence does not replace a prior report or automatically open a new one', async ({ page }) => {
  const options = { unavailable: false };
  await setup(page, options);
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByTestId('analysis-report')).toBeVisible();
  const originalPaths = await page.evaluate(() => (window as any).__mapAudit.paths);
  await page.keyboard.press('Escape');
  options.unavailable = true;
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.407');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('本次步行证据不足，未生成新的体检报告。', { exact: true })).toBeVisible();
  await expect(page.getByTestId('analysis-report')).not.toBeVisible();
  expect(await page.evaluate(() => (window as any).__mapAudit.paths)).toEqual(originalPaths);
  await expect(page.getByText('图层与报告中心：116.404000, 39.915000', { exact: false })).toBeVisible();
  const markers = await page.evaluate(() => (window as any).__mapAudit.markers);
  expect(markers.map((m: any) => [m.point.lng, m.options.title])).toEqual([
    [116.404, '已分析中心（与报告一致）'], [116.407, '待分析选点（BD09LL）'],
  ]);
  await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(page.getByTestId('analysis-report')).toContainText('116.404000, 39.915000');
});

test('creation, real polling stages and fetching have a single waiting flow without percentages', async ({ page }) => {
  let created!: () => void;
  let fetched!: () => void;
  const options = { running: true, createGate: new Promise<void>(resolve => { created = resolve; }),
    resultGate: new Promise<void>(resolve => { fetched = resolve; }) };
  const calls = await setup(page, options);
  await page.goto('/');
  const start = page.getByRole('button', { name: '开始分析', exact: true });
  await start.click();
  const progress = page.getByTestId('analysis-progress');
  await expect(progress).toContainText('正在创建任务');
  await expect(start).toBeDisabled();
  await start.dispatchEvent('click');
  await expect.poll(calls.creations).toBe(1);
  created();
  await expect(progress).toContainText('边界细化与补测');
  await expect(progress).toContainText('200 / 400');
  await expect(progress).not.toContainText(/\d+%/);
  await page.screenshot({ path: test.info().outputPath('api-progress.png'), fullPage: true });
  options.running = false;
  await expect(progress).toContainText('正在整理结果');
  await expect(start).toBeDisabled();
  await expect(page.getByTestId('analysis-report')).not.toBeVisible();
  fetched();
  await expect(page.getByTestId('analysis-report')).toBeVisible();
  await expect(progress).toHaveAttribute('aria-busy', 'false');
  expect(calls.creations()).toBe(1);
});

test('cancel and backend task failure leave the waiting state explicitly', async ({ page }) => {
  const options = { running: true, failed: false };
  await setup(page, options);
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByTestId('analysis-progress')).toContainText('边界细化与补测');
  await page.getByRole('button', { name: '取消任务', exact: true }).click();
  await expect(page.getByText('任务已取消', { exact: true })).toBeVisible();
  await expect(page.getByTestId('analysis-progress')).not.toBeVisible();
  options.failed = true;
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('分析执行失败，请检查后端配置后重试', { exact: true })).toBeVisible();
  await expect(page.getByTestId('analysis-progress')).not.toBeVisible();
  await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();
});

for (const scenario of [
  { createError: 404, message: '分析 API 地址或服务配置异常，请检查服务地址' },
  { statusError: 404, message: '任务不存在或已过期，请重新分析' },
  { createError: 422, message: '分析参数无效，请检查中心坐标和调用预算' },
  { createError: 503, message: '分析服务当前不可用，请联系管理员检查步行服务配置' },
  { createError: 500, message: '分析服务异常，请稍后重试' },
]) {
  test(`request errors exit waiting: ${scenario.message}`, async ({ page }) => {
    await setup(page, scenario);
    await page.goto('/');
    await page.getByRole('button', { name: '开始分析', exact: true }).click();
    await expect(page.getByText(scenario.message, { exact: true })).toBeVisible();
    await expect(page.getByTestId('analysis-progress')).not.toBeVisible();
    await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();
    await expect(page.getByTestId('analysis-report')).not.toBeVisible();
    await expect(page.locator('body')).not.toContainText('private upstream details');
  });
}

test('a structurally valid response for different input is rejected before rendering', async ({ page }) => {
  await setup(page, { mismatch: true });
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('分析结果与提交条件不一致，请检查服务版本', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '查看分析报告', exact: true })).toBeDisabled();
  await expect(page.getByTestId('analysis-report')).not.toBeVisible();
});

test('facility route requests stay on the same origin and draw the returned path', async ({ page }) => {
  await setup(page, { withFacilities: true });
  const savedRoute = { distance_m: 600, duration_s: 500, endpoint_verified: true, reason: null,
    path: [[116.404, 39.915], [116.405, 39.916]] };
  const routeRequests: string[] = [];
  await page.route('**/api/analyses/*/routes/*', route => {
    routeRequests.push(route.request().url());
    return route.fulfill({ json: savedRoute });
  });
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByTestId('analysis-report')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByText('设施与基础报告', { exact: true })).toBeVisible();
  // Legend lists the three facility categories; the route entry appears only after a route is drawn.
  const legend = page.getByTestId('map-legend');
  await expect(legend).toBeVisible();
  await expect(legend).toContainText('购物（菜/超）');
  await expect(legend).toContainText('医疗（药/医）');
  await expect(legend).toContainText('教育（学）');
  await expect(legend).not.toContainText('步行路线');
  // Facility markers carry a categorized canvas icon; center markers keep the default icon.
  const facilityIconSeq = () => page.evaluate(() => {
    const markers = (window as any).__mapAudit.markers as { options: { title: string; icon?: { seq: number } } }[];
    return markers.find(marker => marker.options.title === '离线测试药房')?.options.icon?.seq;
  });
  await expect.poll(facilityIconSeq).toBeTruthy();
  const normalIconSeq = await facilityIconSeq();
  // Selecting the facility in the list swaps in the filled (selected) marker variant.
  await page.getByRole('button', { name: /离线测试药房/ }).click();
  await expect.poll(facilityIconSeq).not.toBe(normalIconSeq);
  await page.getByRole('button', { name: '查看中心到设施的步行路线' }).click();
  await expect(page.getByText('600 米 · 500 秒 · 路线可用；不代表设施已严格核验')).toBeVisible();
  await expect(legend).toContainText('步行路线');
  // The api base is unconfigured here, so the route POST must go to the page origin,
  // never to a hardcoded backend port.
  expect(routeRequests).toHaveLength(1);
  expect(routeRequests[0]).toMatch(/^http:\/\/127\.0\.0\.1:5179\/api\/analyses\/task-1\/routes\/pharmacy-fixture$/);
  const audit = await page.evaluate(() => (window as any).__mapAudit);
  expect(audit.polylines).toEqual([savedRoute.path.map(([lng, lat]) => ({ lng, lat }))]);
  expect(audit.markers.map((marker: any) => marker.options.title)).toContain('离线测试药房');
  // Clicking the facility marker on the map re-selects it and clears the drawn route.
  await page.evaluate(() => {
    const markers = (window as any).__mapAudit.markers as unknown as { options: { title: string }; handlers: { click?: () => void } }[];
    markers.find(marker => marker.options.title === '离线测试药房')?.handlers.click?.();
  });
  await expect(page.locator('.facility-list button.selected', { hasText: '离线测试药房' })).toBeVisible();
  await expect(legend).not.toContainText('步行路线');
  expect(await page.evaluate(() => (window as any).__mapAudit.polylines)).toEqual([]);
  expect(errors).toEqual([]);
});

test('narrow screens keep the facility flow usable without horizontal overflow', async ({ page }) => {
  await setup(page, { withFacilities: true });
  const savedRoute = { distance_m: 600, duration_s: 500, endpoint_verified: true, reason: null,
    path: [[116.404, 39.915], [116.405, 39.916]] };
  await page.route('**/api/analyses/*/routes/*', route => route.fulfill({ json: savedRoute }));
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const report = page.getByTestId('analysis-report');
  await expect(report).toBeVisible();
  await expect(report).toContainText('部分体检结果');
  await page.keyboard.press('Escape');
  const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  // Result, facilities and legend rendered: no horizontal overflow at 390px.
  await expect(page.getByTestId('map-legend')).toBeVisible();
  await expect.poll(overflow).toBeLessThanOrEqual(0);
  const legendBox = await page.getByTestId('map-legend').boundingBox();
  expect(legendBox).toBeTruthy();
  expect(legendBox!.x).toBeGreaterThanOrEqual(0);
  expect(legendBox!.x + legendBox!.width).toBeLessThanOrEqual(390);
  // The map keeps an effective area with controls visible.
  const mapBox = await page.getByTestId('algorithm-map').boundingBox();
  expect(mapBox).toBeTruthy();
  expect(mapBox!.width).toBeGreaterThanOrEqual(280);
  expect(mapBox!.height).toBeGreaterThanOrEqual(300);
  // Facility panel stays operable: select, route request, readable evidence.
  await page.getByRole('button', { name: /离线测试药房/ }).click();
  await page.getByRole('button', { name: '查看中心到设施的步行路线' }).click();
  await expect(page.getByText('600 米 · 500 秒 · 路线可用；不代表设施已严格核验')).toBeVisible();
  await expect.poll(overflow).toBeLessThanOrEqual(0);
  // The report reopens and remains readable on the narrow screen.
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(report).toBeVisible();
  await expect(report).toContainText('116.404000, 39.915000');
  await page.keyboard.press('Escape');
  expect(errors).toEqual([]);
});


test('parsed but offset endpoints never render a valid route or verified POI', async ({ page }) => {
  await setup(page, { withFacilities: true });
  await page.route('**/api/analyses/*/routes/*', route => route.fulfill({ json: {
    endpoint_verified: true, distance_m: 600, duration_s: null, reason: 'endpoint_offset',
    path: [[116.404, 39.915], [116.405, 39.916]],
  } }));
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByTestId('analysis-report')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('analysis-report')).not.toBeVisible();
  await page.getByRole('button', { name: /离线测试药房/ }).click();
  await page.getByRole('button', { name: '查看中心到设施的步行路线' }).click();
  await expect(page.getByText('未知 米 · 未知 秒 · 路线端点偏移，设施可达性未知')).toBeVisible();
  expect(await page.evaluate(() => (window as any).__mapAudit.polylines)).toEqual([]);
  await expect(page.getByText('端点已核验', { exact: false })).toHaveCount(0);
});


for (const [status, label, counts] of [
  ['verified_reachable', '严格核验：15分钟内可达', '15分钟内可达 1'],
  ['verified_unreachable', '严格核验：返回路线超过15分钟', '返回路线超过15分钟 1'],
  ['pending', '待核验（未知）', '待核验（未知）1'],
] as const) {
  test(`production POI evidence ${status} enriches the same report snapshot`, async ({ page }) => {
    await setup(page, { withFacilities: true });
    const pending = status === 'pending';
    const seconds = status === 'verified_unreachable' ? 901 : 500;
    await page.route('**/api/analyses/*/routes/*', route => route.fulfill({ json: {
      endpoint_verified: true, distance_m: 600, duration_s: pending ? null : seconds,
      reason: pending ? 'endpoint_offset' : null, path: [[116.404,39.915],[116.405,39.915]],
      poiEvidence: { version: '1.0', facilityId: 'pharmacy-fixture', status,
        reason: pending ? 'endpoint_offset' : null, duration: pending ? null : seconds,
        observedDuration: seconds, endpointVerified: true,
        requestOrigin: [116.404,39.915], destination: [116.404,39.915], routeOrigin: [116.404,39.915],
        routeDestination: pending ? [116.405,39.915] : [116.404,39.915],
        originOffsetM: 0, destinationOffsetM: pending ? 80 : 0 },
    } }));
    await page.goto('/');
    await page.getByRole('button', { name: '开始分析', exact: true }).click();
    await expect(page.getByTestId('analysis-report')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('analysis-report')).not.toBeVisible();
    await expect(page.getByText('旧版结果未提供严格证据', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: /离线测试药房/ }).click();
    await page.getByRole('button', { name: '查看中心到设施的步行路线' }).click();
    await expect(page.getByTestId('route-poi-evidence')).toContainText(label);
    await expect(page.getByTestId('route-poi-evidence')).toContainText(`实际端点观测耗时：${seconds}`);
    if (pending) expect(await page.evaluate(() => (window as any).__mapAudit.polylines)).toEqual([]);
    await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
    await expect(page.getByTestId('strict-poi-counts')).toContainText(counts);
    await expect(page.getByTestId('analysis-report')).toContainText('116.404000, 39.915000');
  });
}
