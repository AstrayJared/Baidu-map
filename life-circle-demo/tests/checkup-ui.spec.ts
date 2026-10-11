/**
 * v2 体检工作台的界面测试。所有外部请求都被挡下，不用后端也不用真实 AK。
 *
 * 这一套盯的是**图上的行为**，也就是单元测试碰不到的那部分：
 *
 * - 勾选图层是各画各的，摘掉一层不会连累别层（旧页面用的是 `clearOverlays()`）；
 * - 视角只在选点与新结果时移动，勾图层、缩放都不该把用户正在看的地方拽走；
 * - 几百处设施同格合并、数量写在标记上，**一条也不截断**（旧页面有 100 条上限）；
 * - 灰区的空洞照原样画出来：带洞的面积与报告里的数字是同一份几何。
 */
import { test, expect, type Page } from '@playwright/test';
import { installMapSdk } from './mapSdk';
import type { CheckupCompletion } from '../src/checkup/contract';
import { capabilities, collection, feature, layer, point, polygon, report, retainedView, retentionView, retryView, snapshot, task, zone }
  from '../src/checkup/fixtures';

const REVISION = 5;
const HASH = 'hash-5';

for (const width of [1440, 390]) {
  test(`manual continuation preserves report and restores the same round at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await setup(page, { tenCategories: true });
    await page.route('**/api/v2/capabilities', route => {
      const value = capabilities(); value.budgets.poiRequests = 1200;
      return route.fulfill({ json: value });
    });
    let running = false, finished = false, posts = 0;
    const completion = (): CheckupCompletion => ({ reportRevision: finished ? 9 : 5,
      roundNumber: posts ? 2 : 1, roundPoiLimit: posts ? 1200 : 60, roundPoiRequests: running ? 12 : (posts ? 90 : 60),
      cumulativePoiRequests: posts ? (running ? 72 : 150) : 60, routeRequests: 120, routeRemaining: 0,
      queryCompleteByMajor: { medical: false, shopping: true }, evaluatedCategories: 6, totalCategories: 10,
      evaluationStatus: 'partial', canContinue: !running, restartRetrieval: false,
      stopReason: running ? null : 'budget_exhausted', limitations: ['设施检索尚未完成'] });
    await page.route('**/api/v2/checkups/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/continue')) {
        expect(route.request().postDataJSON().baseRevision).toBe(5);
        posts++; running = true;
        return route.fulfill({ status: 202, json: task({ status: 'queued', revision: 5,
          completion: completion() }) });
      }
      if (url.pathname.endsWith('/result')) {
        const revision = Number(url.searchParams.get('revision') || (finished ? 9 : 5));
        return route.fulfill({ json: { ...snapshotFor({ tenCategories: true },
          { center: { lng: 116.405, lat: 39.916 } }), revision, completion: completion(),
          trace: { ...snapshot().trace, resultHash: `hash-${revision}` } } });
      }
      const id = url.pathname.match(/\/layers\/([a-z_]+)$/)?.[1];
      if (id) {
        const revision = Number(url.searchParams.get('revision') || (finished ? 9 : 5));
        return route.fulfill({ json: { ...layerFor(id, {}) as object, revision, resultHash: `hash-${revision}` } });
      }
      return route.fulfill({ json: task({ status: running ? 'running' : 'completed',
        stage: running ? 'poi' : 'ready', revision: finished ? 9 : 5, completion: completion() }) });
    });
    await page.goto('/');
    await page.getByTestId('checkup-map').click();
    await page.getByRole('button', { name: '开始体检', exact: true }).click();
    const reportPanel = page.getByTestId('checkup-report');
    await expect(reportPanel).toBeVisible();
    await expect(page.getByTestId('report-continue')).toContainText('1200');
    await page.getByTestId('report-continue').click();
    await expect(reportPanel).toContainText('正文保留第 5 版报告');
    expect(posts).toBe(1);
    await page.reload();
    await expect(page.getByTestId('checkup-completion').first()).toContainText('第 2 轮');
    expect(posts).toBe(1);
    finished = true; running = false;
    await expect(page.getByTestId('checkup-report')).toBeVisible();
    await expect(page.getByTestId('checkup-report')).not.toContainText('正文保留');
    await expect(page.getByTestId('checkup-report').getByTestId('checkup-completion')).toContainText('累计 150 次');
    expect(posts).toBe(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

/** 带空洞的灰区：面积与报告一致，画的时候少一圈就是两回事。 */
function holed(offset = 0): Record<string, unknown> {
  const [x, y] = [116.39 + offset, 39.89];
  return { type: 'Polygon', coordinates: [
    [[x, y], [x + 0.02, y], [x + 0.02, y + 0.02], [x, y + 0.02], [x, y]],
    [[x + 0.005, y + 0.005], [x + 0.01, y + 0.005], [x + 0.01, y + 0.01], [x + 0.005, y + 0.01],
      [x + 0.005, y + 0.005]],
  ] };
}

type Options = {
  tenCategories?: boolean;
  densityBoundary?: boolean;
  /** 模型网格：西半边三类都覆盖，东半边购物是缺口（其余两类覆盖）。 */
  serviceCells?: boolean;
  facilityCount?: number;
  /** 前两个设施标成同一个疑似重复组（同名同址、相距 20 米内）。 */
  duplicateGroup?: boolean;
  holedGaps?: boolean;
  gapsNotReady?: boolean;
  graphConfigured?: boolean;
  graphState?: 'unloaded' | 'loading' | 'ready' | 'unavailable';
  runningFirst?: boolean;
  /** 设施检索没达标：面板上要出现"继续检索缺口"的入口，并且点得动。 */
  unmetCoverage?: boolean;
  /** 明细已到期：任务视图说明细不可用，明细接口一律 410，保留汇总仍可读。 */
  detailsExpired?: boolean;
};

async function setup(page: Page, options: Options = {}) {
  let submitted: { center: { lng: number; lat: number }; engine: string } | undefined;
  let statusCalls = 0;
  /** 每一次重试请求带上来的标识：用例靠它证明"提交的是同一次重试"。 */
  const retries: string[] = [];
  await installMapSdk(page);
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    // 天气卡片的数据源：测试里给一份固定的实况，别让用例依赖外网。
    if (url.hostname === 'api.open-meteo.com') {
      return route.fulfill({ json: { current: { time: '2026-09-29T18:00', temperature_2m: 21.5,
        relative_humidity_2m: 55, apparent_temperature: 22.1, weather_code: 1, wind_speed_10m: 9.2 } } });
    }
    if (url.hostname !== '127.0.0.1') return route.abort();
    if (!url.pathname.startsWith('/api/v2/')) return route.continue();

    if (url.pathname === '/api/v2/capabilities') {
      return route.fulfill({ json: capabilities({ coverage: {
        graphConfigured: options.graphConfigured ?? true,
        graphState: options.graphState ?? (options.graphConfigured === false ? 'unavailable' : 'ready'),
      } }) });
    }
    if (url.pathname === '/api/v2/checkups') {
      const body = route.request().postDataJSON() as { center: { lng: number; lat: number };
        engine: string; clientRequestId: string };
      submitted = body;
      // 任务视图必须回报**这次请求**的 ID：客户端拿它对账，错一个就会（正确地）拒绝整份响应。
      return route.fulfill({ status: 202, json: task({ status: 'queued', stage: null, revision: 1,
        budget: 400, engine: body.engine, clientRequestId: body.clientRequestId }) });
    }
    // §5 B2 决策 1 的重试：同一张任务上的另一个资源。
    const retryMatch = url.pathname.match(/\/checkups\/([^/]+)\/retries(\/([^/]+))?(\/cancel)?$/);
    if (retryMatch) {
      if (route.request().method() === 'POST' && retryMatch[2] === undefined) {
        const body = route.request().postDataJSON() as { clientRequestId: string };
        retries.push(body.clientRequestId);
        return route.fulfill({ status: 202, json: retryView({ clientRequestId: body.clientRequestId,
          taskId: retryMatch[1], status: 'running', stage: 'poi', finishedAt: null,
          facilitiesStatus: null, stopReason: null, requests: 12, networkRequests: 5,
          budget: { limit: 240, spent: 12, remaining: 228 } }) });
      }
      if (retryMatch[4] !== undefined) {
        return route.fulfill({ json: retryView({ retryId: retryMatch[3], taskId: retryMatch[1],
          status: 'cancelled' }) });
      }
      if (retryMatch[3] !== undefined) {
        // 第一轮已经跑完：任务由此发布了新修订，界面必须跟着换版。
        return route.fulfill({ json: retryView({ retryId: retryMatch[3], taskId: retryMatch[1],
          status: 'partial', facilitiesStatus: 'partial', stopReason: 'network_budget_exhausted' }) });
      }
      return route.fulfill({ json: [] });
    }
    const layerMatch = url.pathname.match(/\/layers\/([a-z_]+)$/);
    if (layerMatch) {
      if (options.detailsExpired) {
        return route.fulfill({ status: 410, json: { code: 'checkup_details_expired',
          message: '这次体检的明细已按保留期到期。' } });
      }
      if (layerMatch[1] === 'service_gaps' && options.gapsNotReady) {
        return route.fulfill({ status: 409, json: { code: 'checkup_service_gaps_not_ready',
          message: '服务灰区图层尚未生成，请等待该阶段完成' } });
      }
      return route.fulfill({ json: layerFor(layerMatch[1], options) });
    }
    if (url.pathname.endsWith('/retained-result')) {
      // 任务视图与保留汇总报的是**同一条**期限：它们在后端出自同一个 retention_of，
      // 这里也必须一致，否则用例会去验证一个真实后端不会产生的组合。
      return route.fulfill({ json: retainedView({ retention: retentionView({
        detailsAvailable: false, reason: 'legacy', cleared: true }) }) });
    }
    if (url.pathname.endsWith('/result')) {
      if (options.detailsExpired) {
        return route.fulfill({ status: 410, json: { code: 'checkup_details_expired',
          message: '这次体检的明细已按保留期到期（它是保留期开始之前的数据）：设施名称、UID、'
            + '地址与坐标不再提供。' } });
      }
      return route.fulfill({ json: snapshotFor(options, submitted!) });
    }
    if (url.pathname.endsWith('/cancel')) {
      return route.fulfill({ json: task({ status: 'cancelled', stage: null, revision: 1 }) });
    }
    if (url.pathname.includes('/routes/')) {
      return route.fulfill({ json: { taskId: 'task-1', revision: REVISION,
        facilityId: 'f-0', category: 'pharmacy', majorCategory: 'medical',
        origin: { lng: 116.405, lat: 39.916 }, destination: { lng: 116.41, lat: 39.92 },
        straightLineM: 700, withinRule: true, routeDistanceM: 805.05, durationS: 670.87,
        observedDurationS: 670.87, poiStatus: 'verified_reachable', poiReason: null,
        evidenceGrade: 'verified', routeOrigin: { lng: 116.405, lat: 39.916 },
        routeDestination: { lng: 116.41, lat: 39.92 }, originOffsetM: 0, destinationOffsetM: 0,
        reason: null, provider: 'offline-fixture', network: true, attempts: 1, budget: {},
        notes: [] } });
    }
    // 任务状态：默认一次就完成；要看阶段推进时，先给一次"进行中"。
    statusCalls++;
    if (options.runningFirst && statusCalls === 1) {
      return route.fulfill({ json: task({ status: 'running', stage: 'poi', revision: 1,
        budget: 400, engine: submitted?.engine ?? 'baidu_e82' }) });
    }
    return route.fulfill({ json: task({ status: 'completed', stage: 'ready', revision: REVISION,
      businessStatus: 'partial', budget: 400, engine: submitted?.engine ?? 'baidu_e82',
      ...(options.detailsExpired ? { retention: retentionView({ detailsAvailable: false,
        reason: 'legacy', cleared: true }) } : {}) }) });
  });
  return { retries };
}

/** 每层的载荷都按后端实际返回的形态给：等时圈是单个面，其余五层是要素集合。 */
function layerFor(id: string, options: Options): unknown {
  const base = { revision: REVISION, resultHash: HASH, document: null };
  if (id === 'isochrone') return layer({ ...base, layerId: 'isochrone', geometry: options.densityBoundary
    ? { type: 'Polygon', coordinates: [
      [[116.403, 39.914], [116.407, 39.914], [116.407, 39.918], [116.403, 39.918], [116.403, 39.914]],
      [[116.4053, 39.9158], [116.4057, 39.9158], [116.4057, 39.9162], [116.4053, 39.9162], [116.4053, 39.9158]],
    ] } : polygon(),
    displayGeometry: polygon() });
  if (id === 'accessibility') return layer({ ...base, layerId: 'accessibility', displayGeometry: null,
    geometry: collection([feature(polygon(0.03), { id: 'domain' })]) });
  if (id === 'service_gaps') {
    return layer({ ...base, layerId: 'service_gaps', displayGeometry: null, geometry: collection(
      options.holedGaps
        ? [feature(holed(), { id: 'zone-0', queryStatus: 'complete' }),
          feature(polygon(0.05), { id: 'zone-1', queryStatus: 'partial' })]
        : [feature(polygon(0.05), { id: 'zone-1', queryStatus: 'partial' })]) });
  }
  if (id === 'facilities') {
    const count = options.facilityCount ?? 2;
    return layer({ ...base, layerId: 'facilities', displayGeometry: null, geometry: collection(
      Array.from({ length: count }, (_, index) => feature(
        // 20 × N 的紧密网格：默认缩放下必然同格，用来验证"合并而不是截断"。
        point(+(116.405 + (index % 10) * 0.00002).toFixed(6),
          +(39.916 + Math.floor(index / 10) * 0.00002).toFixed(6)),
        { id: `f-${index}`, name: `设施 ${index}`,
          majorCategory: index % 3 === 2 ? 'shopping' : 'medical',
          possibleDuplicateGroup: options.duplicateGroup && index < 2 ? 'possible:f0f1' : null }))) });
  }
  if (id === 'heatmap' && options.serviceCells) {
    return layer({ ...base, layerId: 'heatmap', displayGeometry: null,
      geometry: collection(serviceCells(), { metric: 'walking_route', estimated: true, stepM: 50,
        domain: { type: 'Polygon', coordinates: [[[116.403, 39.914], [116.407, 39.914], [116.407, 39.918],
          [116.403, 39.918], [116.403, 39.914]]] } }) });
  }
  if (id === 'verification') {
    return layer({ ...base, layerId: 'verification', displayGeometry: null, geometry: collection([
      feature(point(116.405, 39.916), { facilityId: 'f-0', status: 'verified_reachable' }),
      feature(point(116.406, 39.9165), { facilityId: 'f-1', status: 'verified_unreachable' })]) });
  }
  return layer({ ...base, layerId: id, displayGeometry: null, geometry: collection([]) });
}

/** 50 米格铺满密度测试用的方形圈面；格编号带层级，前端据此还原格边长。 */
function serviceCells(): Record<string, unknown>[] {
  const dLng = 50 / (111_320 * Math.cos((39.916 * Math.PI) / 180));
  const dLat = 50 / 111_320;
  const cells: Record<string, unknown>[] = [];
  for (let i = 0; i < 7; i++) {
    for (let j = 0; j < 9; j++) {
      const lng = 116.403 + (i + 0.5) * dLng;
      const lat = 39.914 + (j + 0.5) * dLat;
      for (const category of ['shopping', 'medical', 'education']) {
        const gap = category === 'shopping' && lng > 116.405;
        cells.push(feature(point(+lng.toFixed(7), +lat.toFixed(7)), { category, cell: `0:${i}:${j}`,
          status: gap ? 'gap' : 'covered', distanceM: gap ? 1400 : 150, nearestFacility: 'f-0' }));
      }
    }
  }
  return cells;
}

function snapshotFor(options: Options, submitted: { center: { lng: number; lat: number } }) {
  const zones = options.holedGaps
    ? [zone({ id: 'zone-0', geometry: holed(), displayGeometry: holed(), reason: 'no_valid_entrance',
      suggestion: '核对通道接入与门牌归属。' }),
      zone({ id: 'zone-1', index: 1, queryStatus: 'partial', geometry: polygon(0.05),
        displayGeometry: polygon(0.05), suggestion: '设施检索未完成，先补采再判定。' })]
    : [zone({ id: 'zone-1', index: 1, queryStatus: 'partial', geometry: polygon(0.05),
      displayGeometry: polygon(0.05), suggestion: '设施检索未完成，先补采再判定。' })];
  const base = snapshot();
  const categories = ['medical', 'shopping', 'education', 'care', 'dining', 'finance',
    'public', 'leisure', 'transport', 'life'];
  const baseReport = report();
  const frozen = options.tenCategories ? report({
    categoryDirectoryVersion: 'test-v1',
    categoryDirectory: categories.map((id, order) => ({ id, label: `测试类别${order + 1}`, order })),
    categories: categories.slice(0, 6).map((id, index) => ({
      ...baseReport.categories[index % baseReport.categories.length], category: id })),
    overall: { ...baseReport.overall!, available: false, reason: 'categories_not_analysed',
      missingCategories: categories.slice(6), coverageLowerPct: null, coverageUpperPct: null,
      assessablePct: null, unknownPct: null },
    gaps: { ...baseReport.gaps, zones },
  }) : report({ gaps: { ...baseReport.gaps, zones } });
  const facilities = options.unmetCoverage
    ? { ...base.facilities!, queryStatus: 'partial' as const, stopReason: 'network_budget_exhausted',
      queryAreaCoverage: { target: 0.8, status: 'unmet', boundaryAreaM2: 1000,
        sharedCompletedAreaM2: 500, sharedCompletionRatio: 0.5, residualRatio: 0.5,
        categories: ['market', 'pharmacy', 'school'] } }
    : base.facilities!;
  return snapshot({
    center: { lng: submitted.center.lng, lat: submitted.center.lat },
    serviceGaps: { ...base.serviceGaps!, zones },
    report: frozen,
    accessibility: { ...base.accessibility!, domain: polygon(0.03) },
    facilities,
  });
}

/** 地图审计：只取当前挂在图上的多边形与标记。 */
const audit = (page: Page) => page.evaluate(() => (window as unknown as { __mapAudit: {
  paths: string[][]; markers: { uid: number; point: { lng: number; lat: number };
    options: { title: string } }[]; pans: { lng: number; lat: number }[]; zooms: string[];
  creations: number; active: number } }).__mapAudit);

/** 标题里的点数：同格合并时写的是这一格的数量，没有数字的按一个算。 */
function counted(markers: { options: { title: string } }[]): number {
  return markers.reduce((sum, marker) =>
    sum + (Number(/^(\d+) 个点/.exec(marker.options.title)?.[1]) || 1), 0);
}

const openTab = (page: Page, tab: '我的位置' | '采样与引擎' | '图层备注') =>
  page.getByRole('tab', { name: tab }).click();

const pickAndStart = async (page: Page,
  options: { serviceGaps?: boolean; done?: string } = {}) => {
  if (options.serviceGaps) {
    await openTab(page, '图层备注');
    await page.getByRole('checkbox', { name: '服务灰区', exact: true }).check();
  }
  await page.getByTestId('checkup-map').click();
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  // 等这一轮结束：默认等报告，明细到期的用例等的是那一栏说明 —— 那时候没有报告可等。
  await expect(page.getByTestId(options.done ?? 'checkup-report')).toBeVisible();
  await page.keyboard.press('Escape');
};

test('设施密度绘出非透明像素、孔洞保持透明，切换与缩放后仍正确', async ({ page }) => {
  await setup(page, { densityBoundary: true, facilityCount: 1 });
  await page.goto('/');
  await openTab(page, '图层备注');
  // 设施密度不是默认热力：要自己打开，打开后服务覆盖热力随之关闭（两者互斥）。
  await page.getByRole('checkbox', { name: '设施密度热力', exact: true }).check();
  await expect(page.getByRole('checkbox', { name: '服务覆盖热力', exact: true })).not.toBeChecked();
  // 关闭独立点位与圈面显示后，密度仍须获取依赖数据。
  await page.getByRole('checkbox', { name: '设施点位', exact: true }).uncheck();
  await page.getByRole('checkbox', { name: '步行等时圈', exact: true }).uncheck();
  await pickAndStart(page);
  const canvas = page.getByTestId('facility-density-canvas');
  const alphaAt = (lng: number, lat: number) => canvas.evaluate((el, pos) => {
    const audit = (window as unknown as { __mapAudit: {
      project: (lng: number, lat: number) => { x: number; y: number } } }).__mapAudit;
    const pixel = audit.project(pos.lng, pos.lat);
    const c = el as HTMLCanvasElement;
    const x = (pixel.x - parseFloat(c.style.left)) * c.width / parseFloat(c.style.width);
    const y = (pixel.y - parseFloat(c.style.top)) * c.height / parseFloat(c.style.height);
    return c.getContext('2d')!.getImageData(Math.round(x), Math.round(y), 1, 1).data[3];
  }, { lng, lat });
  await expect(canvas).toBeVisible();
  await expect.poll(() => alphaAt(116.405, 39.916)).toBeGreaterThan(0);
  expect(await alphaAt(116.4055, 39.916)).toBe(0);
  expect(await alphaAt(116.4075, 39.916)).toBe(0);
  const pans = (await audit(page)).pans.length;
  await page.getByRole('checkbox', { name: '设施密度热力', exact: true }).uncheck();
  await expect(canvas).toHaveCount(0);
  await page.getByRole('checkbox', { name: '设施密度热力', exact: true }).check();
  await expect.poll(() => alphaAt(116.405, 39.916)).toBeGreaterThan(0);
  await page.evaluate(() => {
    const audit = (window as unknown as { __mapAudit: { views: Record<string, (() => void)[]> } }).__mapAudit;
    for (const type of ['moveend', 'zoomend', 'resize']) for (const handler of audit.views[type] ?? []) handler();
  });
  await expect.poll(() => alphaAt(116.405, 39.916)).toBeGreaterThan(0);
  expect(await alphaAt(116.4055, 39.916)).toBe(0);
  expect((await audit(page)).pans.length).toBe(pans);
  await page.locator('.api-map-shell').screenshot({ path: 'output/checkup-ui/density-regression.png' });
});

/** 画布上某个经纬度处的 RGBA：与覆盖物用同一个投影，按画布的 CSS 位置与设备像素比换算。 */
const rgbaAt = (canvas: ReturnType<Page['getByTestId']>, lng: number, lat: number) => canvas.evaluate((el, pos) => {
  const audit = (window as unknown as { __mapAudit: {
    project: (lng: number, lat: number) => { x: number; y: number } } }).__mapAudit;
  const pixel = audit.project(pos.lng, pos.lat);
  const c = el as HTMLCanvasElement;
  const x = (pixel.x - parseFloat(c.style.left)) * c.width / parseFloat(c.style.width);
  const y = (pixel.y - parseFloat(c.style.top)) * c.height / parseFloat(c.style.height);
  return [...c.getContext('2d')!.getImageData(Math.round(x), Math.round(y), 1, 1).data];
}, { lng, lat });

const near = (actual: number[], expected: number[], tolerance = 6) =>
  expected.every((value, index) => Math.abs(actual[index] - value) <= tolerance);


test('设施密度按类别筛选、疑似重复只算一处，空类别明说，刷新后筛选仍在', async ({ page }) => {
  await setup(page, { densityBoundary: true, facilityCount: 3, duplicateGroup: true });
  await page.goto('/');
  await openTab(page, '图层备注');
  await page.getByRole('checkbox', { name: '设施密度热力', exact: true }).check();
  await pickAndStart(page);
  const legend = page.getByTestId('density-legend');
  const canvas = page.getByTestId('facility-density-canvas');
  // f-0 与 f-1 是同一组疑似重复：三条记录只画两处，图例说出合并了几条。
  await expect(legend).toContainText('2 处设施参与（1 条疑似重复已合并）');
  await expect(legend).toContainText('竖线：单个设施中心 0.66');
  await expect(legend).toHaveAttribute('data-points', '2');
  await expect.poll(async () => (await rgbaAt(canvas, 116.405, 39.916))[3]).toBeGreaterThan(0);

  const choose = async (label: string) => {
    await page.getByRole('combobox', { name: '密度类别' }).click();
    await page.locator('.ant-select-dropdown:visible').getByTitle(label, { exact: true }).click();
  };
  // 本次没有教育设施：说出来，画布清空，而不是留着上一类的颜色。
  await choose('教育');
  await expect(legend).toContainText('教育密度');
  await expect(legend).toContainText('本次没有教育设施');
  await expect(legend).toHaveAttribute('data-points', '0');
  await expect.poll(async () => (await rgbaAt(canvas, 116.405, 39.916))[3]).toBe(0);
  // 只看购物：只剩 f-2 这一处；单个设施中心的颜色在浅底上也看得出（不透明度 ≥ 0.4）。
  await choose('购物消费');
  await expect(legend).toContainText('1 处设施参与');
  await expect(legend).not.toContainText('疑似重复');
  await expect.poll(async () => (await rgbaAt(canvas, 116.40504, 39.916))[3]).toBeGreaterThanOrEqual(100);
  // 刷新：任务按保存的标识恢复，筛选按离开时的样子。
  await page.reload();
  await expect(page.getByRole('combobox', { name: '密度类别' })).toBeVisible();
  await expect(page.locator('.ant-select').filter({ has: page.getByRole('combobox', { name: '密度类别' }) }))
    .toContainText('购物消费');
  await expect(legend).toContainText('1 处设施参与');
  await expect.poll(async () => (await rgbaAt(canvas, 116.40504, 39.916))[3]).toBeGreaterThanOrEqual(100);
  await page.locator('.api-map-shell').screenshot({ path: 'output/checkup-ui/density-category.png' });
});
test('服务覆盖热力默认打开：评估格连成渐变面，圈外与孔洞透明，切类别、切热力都各归其位', async ({ page }) => {
  await setup(page, { densityBoundary: true, serviceCells: true });
  await page.goto('/');
  // 热力与叠加开关在「图层备注」选项卡里。
  await openTab(page, '图层备注');
  // 模型网格采样点位本身不勾：热力仍须取到模型网格（与密度热力取设施同理）。
  await expect(page.getByRole('checkbox', { name: '模型网格采样', exact: true })).not.toBeChecked();
  await expect(page.getByRole('checkbox', { name: '服务灰区', exact: true })).not.toBeChecked();
  await expect(page.getByRole('checkbox', { name: '服务覆盖热力', exact: true })).toBeChecked();
  await pickAndStart(page);
  const canvas = page.getByTestId('service-heat-canvas');
  await expect(canvas).toBeVisible();
  await expect(page.getByTestId('facility-density-canvas')).toHaveCount(0);
  // 综合：西半边三类都覆盖 → 满分绿；东半边购物缺口 → 三分之二处的黄。
  await expect.poll(async () => (await rgbaAt(canvas, 116.4035, 39.915))[3]).toBeGreaterThan(0);
  const west = await rgbaAt(canvas, 116.4035, 39.915);
  const east = await rgbaAt(canvas, 116.4065, 39.915);
  expect(near(west, [26, 152, 80]), `west ${west}`).toBe(true);
  expect(near(east, [254, 224, 139]), `east ${east}`).toBe(true);
  // 计算圈的孔洞与圈外一律透明：热力不越过圈面。
  expect((await rgbaAt(canvas, 116.4055, 39.916))[3]).toBe(0);
  expect((await rgbaAt(canvas, 116.4075, 39.916))[3]).toBe(0);
  await expect(page.getByTestId('service-legend')).toContainText('全部类别均已知');
  await expect(page.getByTestId('service-legend')).toContainText('模型估计');
  // 开着热力时等时圈只描边：面填色会透过半透明热力把整圈染成一片。
  const fills = async () => (await page.evaluate(() => (window as unknown as { __mapAudit: {
    fills: { fillOpacity: number | null }[] } }).__mapAudit.fills)).map(fill => fill.fillOpacity);
  await expect.poll(fills).toContain(0);
  expect(await fills()).not.toContain(0.26);

  // 单看购物：东半边是缺口灰，西半边按距离着色（150 米，偏绿）。
  await page.getByRole('combobox', { name: '覆盖类别' }).click();
  await page.locator('.ant-select-dropdown:visible .ant-select-item-option').filter({ hasText: '购物' }).first().click();
  await expect.poll(async () => near(await rgbaAt(canvas, 116.4065, 39.915), [107, 114, 128])).toBe(true);
  const shoppingWest = await rgbaAt(canvas, 116.4035, 39.915);
  expect(shoppingWest[1]).toBeGreaterThan(shoppingWest[0]);
  await expect(page.getByTestId('service-legend')).toContainText('购物');

  // 两种热力互斥：打开密度，服务覆盖画布摘掉；两种都关掉，圈面恢复填色。
  await page.getByRole('checkbox', { name: '设施密度热力', exact: true }).check();
  await expect(canvas).toHaveCount(0);
  await expect(page.getByRole('checkbox', { name: '服务覆盖热力', exact: true })).not.toBeChecked();
  await page.getByRole('checkbox', { name: '设施密度热力', exact: true }).uncheck();
  await expect.poll(fills).toContain(0.26);
  await page.getByRole('checkbox', { name: '服务覆盖热力', exact: true }).check();
  await expect.poll(async () => near(await rgbaAt(canvas, 116.4065, 39.915), [107, 114, 128])).toBe(true);
  await page.locator('.api-map-shell').screenshot({ path: 'output/checkup-ui/service-heat.png' });
});

/**
 * 图层是一层一层取的，报告先出来、图后画完。所以断言图上的东西之前要先等到位 ——
 * 直接读审计数组测的是"此刻画到哪了"，不是"最终画成了什么"。
 */
const pathsAre = (page: Page, count: number) =>
  expect.poll(async () => (await audit(page)).paths.length).toBe(count);
const markersAre = (page: Page, count: number) =>
  expect.poll(async () => (await audit(page)).markers.length).toBe(count);
const pointsAre = (page: Page, count: number) =>
  expect.poll(async () => counted((await audit(page)).markers)).toBe(count);

for (const width of [1280, 390]) {
  test(`report conclusions and navigation remain readable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await setup(page);
    await page.goto('/');
    await page.getByTestId('checkup-map').click();
    await page.getByRole('button', { name: '开始体检', exact: true }).click();
    const summary = page.getByTestId('report-summary');
    await expect(summary).toBeVisible();
    await expect(summary).toContainText('40.0% ～ 70.0%');
    await expect(summary).toContainText('已尝试核验 4 处设施');
    const hash = new URL(page.url()).hash;
    await page.getByRole('navigation', { name: '报告目录' }).getByRole('button', { name: '服务盲区' }).click();
    expect(new URL(page.url()).hash).toBe(hash);
    await expect(page.getByTestId('zone-zone-1')).toContainText('建议下一步：');
    expect(await summary.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  });
}

for (const width of [1280, 390]) {
  test(`ten-category frozen report names missing categories at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await setup(page, { tenCategories: true });
    await page.goto('/');
    await page.getByTestId('checkup-map').click();
    await page.getByRole('button', { name: '开始体检', exact: true }).click();
    const reportView = page.getByTestId('checkup-report');
    await expect(reportView).toBeVisible();
    await expect(reportView.locator('li[data-testid^="coverage-"]')).toHaveCount(10);
    await expect(reportView.getByTestId('coverage-life')).toContainText('无法给出覆盖率');
    await expect(reportView.getByTestId('coverage-life')).toContainText('设施检索未完成');
    await expect(reportView.getByText('暂无法评估', { exact: true })).toBeVisible();
    await page.screenshot({ path: `output/checkup-ui/ten-categories-${width}.png` });
    await page.getByRole('navigation', { name: '报告目录' })
      .getByRole('button', { name: '分类覆盖' }).click();
    await reportView.getByTestId('coverage-life').scrollIntoViewIfNeeded();
    await page.screenshot({ path: `output/checkup-ui/ten-categories-detail-${width}.png` });
  });
}

test('a published revision draws its layers, opens the report and keeps the view still', async ({ page }) => {
  const errors: string[] = [];
  await setup(page);
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await openTab(page, '采样与引擎');
  await expect(page.getByTestId('quota-label'))
    .toHaveText('本应用请求限制（不含浏览器 SDK、其他应用及旧接口流量）');
  // 选点：只会平移这一次；后面取图层、画标记都不再动视角。
  await pickAndStart(page, { serviceGaps: true });
  const report = page.getByTestId('checkup-report');
  await expect(report).toContainText('15 分钟生活圈体检报告');
  await expect(report).toContainText('task-1 · 第 5 版');
  await expect(page.getByTestId('coverage-shopping')).toContainText('40.0%');
  await expect(page.getByTestId('coverage-shopping')).toContainText('70.0%');
  // 灰区清单照抄后端给的说法：它是"要做的事"，不是界面重新措辞的提示。
  await expect(page.getByTestId('zone-zone-1')).toContainText('设施检索未完成，先补采再判定。');
  await expect(page.getByTestId('verification-summary')).toContainText('已尝试核验 4 处设施');

  const drawn = await audit(page);
  // 等时圈 1 + 评估域 1 + 灰区 1：三层各画各的，谁也不清空谁。
  await pathsAre(page, 3);
  expect(drawn.creations).toBe(1);
  expect(drawn.pans.map(pan => [pan.lng, pan.lat])).toEqual([[116.405, 39.916]]);
  expect(errors).toEqual([]);
});

test('layers are independent: unchecking one leaves the others on the map', async ({ page }) => {
  await setup(page, { facilityCount: 3 });
  await page.goto('/');
  await openTab(page, '图层备注');
  await pickAndStart(page, { serviceGaps: true });
  await pathsAre(page, 3);
  // 3 处设施合成 1 枚 + 2 处核验 + 1 枚中心标记。
  await markersAre(page, 4);
  await page.getByRole('checkbox', { name: '服务灰区', exact: true }).uncheck();
  await pathsAre(page, 2);
  // 设施点位不受影响：摘掉一层不该顺手把别层也摘了。
  await markersAre(page, 4);
  await page.getByRole('checkbox', { name: '服务灰区', exact: true }).check();
  await pathsAre(page, 3);
});

test('hundreds of facilities are merged by cell, never truncated', async ({ page }) => {
  await setup(page, { facilityCount: 200 });
  await page.goto('/');
  await pickAndStart(page);
  // 200 处设施 + 2 处核验 + 1 枚中心标记：合并之后标记远少于点数，但总数一个不少 ——
  // 这正是"聚合而不是截断"（中心标记不写数量，按一个算，所以这里连它一起数）。
  await pointsAre(page, 203);
  const drawn = await audit(page);
  expect(drawn.markers.length).toBeLessThan(20);
  const merged = drawn.markers.filter(marker => /^\d+ 个点/.test(marker.options.title));
  expect(merged.length).toBeGreaterThan(0);
  expect(Number(/^(\d+) 个点/.exec(merged[0].options.title)![1])).toBeGreaterThan(1);
  await expect(page.getByTestId('checkup-legend')).toContainText('同格合并显示，数据不截断');
});

test('a grey zone keeps its hole, and the partial one is drawn as a different conclusion', async ({ page }) => {
  await setup(page, { holedGaps: true });
  await page.goto('/');
  await pickAndStart(page, { serviceGaps: true });
  // 等时圈 1 + 评估域 1 + 两个灰区 = 4 个覆盖物，其中带洞的那个有两圈。
  await pathsAre(page, 4);
  const drawn = await audit(page);
  expect(drawn.paths.filter(rings => rings.length === 2)).toHaveLength(1);
  await expect(page.getByTestId('checkup-legend')).toContainText('服务灰区');
});

test('zoom and pan end re-project the points without moving the view or dropping layers', async ({ page }) => {
  await setup(page, { facilityCount: 40 });
  await page.goto('/');
  await pickAndStart(page, { serviceGaps: true });
  await pathsAre(page, 3);
  // 等到全部点都画上：40 处设施 + 2 处核验 + 1 枚中心标记。按点数等而不按标记枚数等 ——
  // 这一簇合成几枚取决于格线落在哪，而格线随地图容器的宽度移动，与这里要测的事无关。
  await pointsAre(page, 43);
  const before = await audit(page);
  // 真实 SDK 在缩放/平移结束时会发事件：点图层据此重新投影，面不动，视角也不该被拽走。
  await page.evaluate(() => {
    const audit = (window as unknown as { __mapAudit: { views: Record<string, (() => void)[]> } }).__mapAudit;
    for (const type of ['zoomend', 'moveend']) for (const handler of audit.views[type] ?? []) handler();
  });
  await expect.poll(async () => {
    const current = await audit(page);
    return current.markers.filter((marker, index) => marker.uid !== before.markers[index]?.uid).length;
  }).toBeGreaterThan(0);
  const after = await audit(page);
  // 点图层重新投影过：设施与核验的标记是新建的（中心标记不动，所以不是"全都换了"）。
  expect(after.markers.filter((marker, index) => marker.uid !== before.markers[index]?.uid).length)
    .toBeGreaterThan(0);
  expect(after.markers).toHaveLength(before.markers.length);
  expect(counted(after.markers)).toBe(counted(before.markers));
  // 面是按地理坐标画的，视图变化不影响它们：既没重画，也没被清掉。
  expect(after.paths).toEqual(before.paths);
  expect(after.pans).toEqual(before.pans);
  // 重新投影过：标记重建了，但数量与内容一致。
  expect(after.markers.map(marker => marker.uid))
    .not.toEqual(before.markers.map(marker => marker.uid));
});

test('中心标记跟随坐标输入，+/- 按钮走 SDK 的 zoomIn/zoomOut', async ({ page }) => {
  await setup(page);
  await page.goto('/');
  // 手输坐标也算一次选点：中心标记与视野立刻跟到最后一个有效值。
  await page.getByRole('spinbutton', { name: '经度' }).fill('116.418000');
  await page.getByRole('spinbutton', { name: '纬度' }).fill('39.923000');
  await expect.poll(async () => {
    const marker = (await audit(page)).markers.find(item => item.options.title.startsWith('待体检选点'));
    return marker ? { lng: marker.point.lng, lat: marker.point.lat } : null;
  }).toEqual({ lng: 116.418, lat: 39.923 });
  expect((await audit(page)).pans.at(-1)).toEqual({ lng: 116.418, lat: 39.923 });
  // 缩放按钮在地图右上角：替身记下调用的是哪一个，不模拟真实级别。
  const pansBefore = (await audit(page)).pans.length;
  await page.getByRole('button', { name: '放大' }).click();
  await page.getByRole('button', { name: '缩小' }).click();
  await expect.poll(async () => (await audit(page)).zooms).toEqual(['in', 'out']);
  // 点按钮不允许穿透成"在地图上选点"：选点与视野都不变。
  expect((await audit(page)).pans.length).toBe(pansBefore);
  expect((await audit(page)).pans.at(-1)).toEqual({ lng: 116.418, lat: 39.923 });
  await page.locator('.api-map-shell').screenshot({ path: 'output/checkup-ui/zoom-controls.png' });
});

test('天气卡片报中心实况，设施清单按类别列出最近的设施', async ({ page }) => {
  await setup(page, { facilityCount: 12 });
  await page.goto('/');
  const weather = page.getByTestId('weather-card');
  await expect(page.locator('.wb-results')).toBeVisible();
  await expect(weather).toHaveAttribute('data-status', 'ready', { timeout: 10000 });
  await expect(weather).toContainText('21.5');
  await expect(weather).toContainText('大致晴朗');
  await expect(weather).toContainText('湿度 55%');
  const atPanelBottom = () => weather.evaluate(element => {
    const container = element.parentElement;
    return container?.classList.contains('wb-weather-bottom')
      && container.parentElement?.lastElementChild === container;
  });
  expect(await atPanelBottom()).toBe(true);

  await pickAndStart(page);
  const nearest = page.getByTestId('checkup-nearest');
  await expect(nearest).toContainText('购物');
  await expect(nearest).toContainText('医疗');
  await expect(nearest.locator('.wb-near-list button').first()).toBeVisible();
  expect(await atPanelBottom()).toBe(true);
  // 每类最多 5 处；12 条记录里购物 4、医疗 8，合计 9 条。
  expect(await nearest.locator('.wb-near-list button').count()).toBe(9);
  // 点一处设施即选中：设施详情随之出现，可继续查步行路线。
  await nearest.locator('.wb-near-list button').first().click();
  await expect(page.getByRole('button', { name: '查询步行路线' })).toBeVisible();
  await page.locator('.wb-results').screenshot({ path: 'output/checkup-ui/nearest-weather.png' });
});

test('左右面板可拖动和缩放，且保持在地图范围内', async ({ page }) => {
  await setup(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');

  const drag = async (handle: ReturnType<Page['getByTestId']>, dx: number, dy: number) => {
    const box = await handle.boundingBox();
    expect(box).not.toBeNull();
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.mouse.down();
    await page.mouse.move(box!.x + box!.width / 2 + dx, box!.y + box!.height / 2 + dy, { steps: 5 });
    await page.mouse.up();
  };

  const side = page.locator('.wb-side');
  const sideStart = await side.boundingBox();
  expect(sideStart).not.toBeNull();
  await drag(page.getByTestId('panel-move-side'), 64, 48);
  await drag(page.getByTestId('panel-resize-side'), 48, 36);
  const sideEnd = await side.boundingBox();
  expect(sideEnd!.x).toBeGreaterThan(sideStart!.x + 40);
  expect(sideEnd!.y).toBeGreaterThan(sideStart!.y + 24);
  expect(sideEnd!.width).toBeGreaterThan(sideStart!.width + 24);
  expect(sideEnd!.height).toBeGreaterThan(sideStart!.height + 20);
  await drag(page.getByTestId('panel-resize-side'), 2000, 2000);
  const sideMax = await side.boundingBox();
  expect(sideMax!.width).toBe(480);
  expect(sideMax!.height).toBe(760);
  await drag(page.getByTestId('panel-resize-side'), -2000, -2000);
  const sideMin = await side.boundingBox();
  expect(sideMin!.width).toBe(300);
  expect(sideMin!.height).toBe(280);

  const results = page.locator('.wb-results');
  const resultStart = await results.boundingBox();
  expect(resultStart).not.toBeNull();
  await drag(page.getByTestId('panel-move-results'), -72, 40);
  await drag(page.getByTestId('panel-resize-results'), 72, 48);
  const resultEnd = await results.boundingBox();
  const workspace = await page.locator('.wb-body').boundingBox();
  expect(resultEnd!.x).toBeLessThan(resultStart!.x - 40);
  expect(resultEnd!.width).toBeGreaterThan(resultStart!.width + 40);
  expect(resultEnd!.height).toBeGreaterThan(resultStart!.height + 30);
  expect(resultEnd!.x).toBeGreaterThanOrEqual(workspace!.x + 15);
  expect(resultEnd!.y).toBeGreaterThanOrEqual(workspace!.y + 15);
  expect(resultEnd!.x + resultEnd!.width).toBeLessThanOrEqual(workspace!.x + workspace!.width - 15);
  expect(resultEnd!.y + resultEnd!.height).toBeLessThanOrEqual(workspace!.y + workspace!.height - 15);
  await drag(page.getByTestId('panel-resize-results'), 2000, 2000);
  const resultMax = await results.boundingBox();
  expect(resultMax!.width).toBe(520);
  expect(resultMax!.height).toBe(760);
  expect(resultMax!.x + resultMax!.width).toBeLessThanOrEqual(workspace!.x + workspace!.width - 15);
  expect(resultMax!.y + resultMax!.height).toBeLessThanOrEqual(workspace!.y + workspace!.height - 15);
  await drag(page.getByTestId('panel-resize-results'), -2000, -2000);
  const resultMin = await results.boundingBox();
  expect(resultMin!.width).toBe(320);
  expect(resultMin!.height).toBe(280);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId('panel-move-results')).toHaveCount(0);
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect.poll(async () => {
    const sideBox = await side.boundingBox();
    const resultsBox = await results.boundingBox();
    return sideBox && resultsBox ? resultsBox.x > sideBox.x + sideBox.width : false;
  }).toBe(true);
});

test('a layer that is not ready says so by name, and the rest still draw', async ({ page }) => {
  await setup(page, { gapsNotReady: true });
  await page.goto('/');
  await pickAndStart(page, { serviceGaps: true });
  await openTab(page, '图层备注');
  // 后端的原话照登：把 409 说成"这一层是空的"，读者会以为灰区已经查过了。
  await expect(page.getByText('服务灰区图层尚未生成，请等待该阶段完成', { exact: true })).toBeVisible();
  await pathsAre(page, 2);
});

test('stages advance as the backend reports them, and the engines come from the service', async ({ page }) => {
  await setup(page, { runningFirst: true, graphConfigured: false });
  await page.goto('/');
  // 还没提交时没有阶段可显示：进度条不是"空着等"，是没有这一栏。
  await expect(page.getByTestId('checkup-stages')).toHaveCount(0);
  await openTab(page, '采样与引擎');
  await pickAndStart(page);
  const stages = page.getByTestId('checkup-stages');
  await expect(stages).toBeVisible();
  for (const label of ['等时圈', '设施检索', '服务覆盖', '步行核验', '报告', '完成']) {
    await expect(stages.locator('li', { hasText: label })).toHaveAttribute('data-reached', 'yes');
  }
  // 档位是引擎自己带来的，不是界面写死的三档。
  // antd 的下拉项由虚拟列表渲染，可见性判定不稳，这里断言挂载与文本 —— 那才是"能选什么"。
  await expect(page.getByTestId('checkup-time-estimate')).toHaveCount(0);
  await expect(page.getByText('实际用时受网络影响', { exact: true })).toBeVisible();
  await page.getByRole('combobox', { name: '调用预算' }).click();
  await expect(page.getByRole('option', { name: '200 次', exact: true })).toBeAttached();
  await expect(page.getByRole('option', { name: '400 次', exact: true })).toBeAttached();
  await expect(page.getByRole('option', { name: '800 次', exact: true })).toBeAttached();
  await page.keyboard.press('Escape');
  // 两个引擎都列着：路网没配好只影响后端的取舍，界面不替它隐藏其中一个。
  // 引擎名取自能力表，不是界面写死的。
  await expect(page.getByTestId('checkup-engine')).toContainText('引擎：百度边界搜索（E8.2）');
  await page.getByTestId('algorithm-hybrid').click();
  await expect(page).toHaveURL(/#\/checkup\/hybrid$/);
  await expect(page.getByTestId('checkup-engine')).toContainText('引擎：OSM＋百度');
  await expect(page.getByTestId('checkup-time-estimate')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '开始体检', exact: true })).toBeEnabled();
});


async function expectLegendDock(page: Page) {
  await expect.poll(async () => {
    const [side, legend, workspace, report] = await Promise.all([
      page.locator('.wb-side').boundingBox(), page.getByTestId('checkup-legend').boundingBox(),
      page.locator('.wb-body').boundingBox(), page.locator('.wb-results').boundingBox(),
    ]);
    return !!(side && legend && workspace && report
      && Math.abs(side.x - legend.x) < 1 && Math.abs(side.width - legend.width) < 1
      && Math.abs(legend.y - side.y - side.height - 12) < 1
      && legend.y + legend.height <= workspace.y + workspace.height - 27
      && legend.x + legend.width <= report.x);
  }).toBe(true);
}

for (const [width, height] of [[1280, 720], [1440, 900], [2048, 1066]]) {
  test(`legend docks below the left panel without report overlap at ${width}x${height}`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await setup(page, { tenCategories: true, serviceCells: true, facilityCount: 12 });
    await page.goto('/');
    await pickAndStart(page);
    await expect(page.getByTestId('checkup-report')).not.toBeVisible();
    await expect(page.getByTestId('checkup-legend')).toBeVisible();
    await expectLegendDock(page);
    await page.screenshot({ path: `output/checkup-ui/legend-dock-${width}x${height}.png` });
    const before = await page.locator('.wb-side').boundingBox();
    await page.getByTestId('checkup-legend').getByRole('button', { name: '图例', exact: true }).click();
    await expect(page.getByTestId('checkup-legend')).toHaveAttribute('data-open', 'no');
    await expectLegendDock(page);
    const collapsed = await page.locator('.wb-side').boundingBox();
    expect(collapsed!.height).toBeGreaterThanOrEqual(before!.height);
    await page.getByTestId('checkup-legend').getByRole('button', { name: '图例', exact: true }).click();
    await expectLegendDock(page);
  });
}

test('legend follows drag, resize and keyboard controls through viewport changes', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await setup(page, { tenCategories: true, serviceCells: true, facilityCount: 12 });
  await page.goto('/'); await pickAndStart(page);
  await expect(page.getByTestId('checkup-report')).not.toBeVisible();
  await expectLegendDock(page);
  const drag = async (id: string, dx: number, dy: number) => {
    const box = await page.getByTestId(id).boundingBox();
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.mouse.down();
    await page.mouse.move(box!.x + box!.width / 2 + dx, box!.y + box!.height / 2 + dy, { steps: 5 });
    await page.mouse.up();
  };
  await drag('panel-move-side', 40, 35); await expectLegendDock(page);
  await drag('panel-resize-side', 72, 2000); await expectLegendDock(page);
  await page.getByTestId('panel-move-side').press('ArrowDown'); await expectLegendDock(page);
  await page.getByTestId('panel-resize-side').press('ArrowLeft'); await expectLegendDock(page);
  await page.setViewportSize({ width: 1280, height: 720 }); await expectLegendDock(page);
  await page.setViewportSize({ width: 1440, height: 560 }); await expectLegendDock(page);
  await page.setViewportSize({ width: 1440, height: 500 });
  await expect(page.getByTestId('panel-move-side')).toHaveCount(0);
  await expect(page.locator('.wb-side')).toHaveCSS('position', 'static');
  const legend = await page.getByTestId('checkup-legend').boundingBox();
  const map = await page.getByTestId('checkup-map-section').boundingBox();
  const side = await page.locator('.wb-side').boundingBox();
  expect(legend!.y + legend!.height).toBeLessThan(map!.y + map!.height);
  expect(side!.y).toBeGreaterThanOrEqual(map!.y + map!.height);
  await page.screenshot({ path: 'output/checkup-ui/legend-dock-low-height.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId('panel-move-results')).toHaveCount(0);
  await expect(page.getByTestId('checkup-legend')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'output/checkup-ui/legend-dock-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 }); await expectLegendDock(page);
});

test('hybrid shows its historical runtime guide only when the graph is ready', async ({ page }) => {
  await setup(page);
  await page.goto('/');
  await openTab(page, '采样与引擎');
  await page.getByTestId('algorithm-hybrid').click();
  await expect(page).toHaveURL(/#\/checkup\/hybrid$/);
  await expect(page.getByTestId('checkup-time-estimate')).toHaveText(/3–7 分钟/);
});

test('hybrid hides the runtime guide while the graph is still loading', async ({ page }) => {
  await setup(page, { graphState: 'loading' });
  await page.goto('/');
  await openTab(page, '采样与引擎');
  await page.getByTestId('algorithm-hybrid').click();
  await expect(page).toHaveURL(/#\/checkup\/hybrid$/);
  await expect(page.getByTestId('checkup-time-estimate')).toHaveCount(0);
});

/**
 * §5 B2 决策 1 的重试入口。单元测试与控制器测试各自钉住了判决文案和时序，这里钉的是
 * **它真的出现在面板上、真的点得动**：未达标时给出下一步，点一次就提交一次重试，
 * 而且提交的请求体里只有标识 —— 类别与圈面由后端从被冻结的那一版取。
 */
test('未达标时给出继续检索入口，点一次就提交一次重试', async ({ page }) => {
  const errors: string[] = [];
  const harness = await setup(page, { unmetCoverage: true });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await pickAndStart(page);

  // 先说清"查完了多少"，再说下一步：达标线是 80%，实测 50%。
  await expect(page.getByTestId('checkup-query-coverage'))
    .toContainText('共同完成圈面 50.0%（合格线 80.0%）');
  await expect(page.getByTestId('checkup-query-coverage')).toContainText('未达标');
  // 原因按真实停止原因区分：本轮额度用尽可以再点一次，今日额度用尽要等次日。
  await expect(page.getByTestId('checkup-query-coverage-next'))
    .toContainText('本轮请求额度已用完，可重试继续');

  await page.getByTestId('checkup-retry-run').click();
  // 这一轮**自己的**账目：预算属于这次重试，不是原任务的。
  await expect(page.getByTestId('checkup-retry-facts').first()).toContainText('本轮预算 240');
  expect(harness.retries).toHaveLength(1);
  expect(harness.retries[0]).toMatch(/^[0-9a-f-]{36}$/);
  expect(errors).toEqual([]);
});

/**
 * §5 B2 决策 2 的到期反应。单元测试钉住了时序，这里钉的是**屏幕上真的没有明细**：
 * 到期之后不能再有任何设施、图层或报告，只剩说明与保留汇总 —— 而且"到期"不是"体检失败"。
 */
test('明细到期后只剩结论与汇总，一个设施都不显示', async ({ page }) => {
  const errors: string[] = [];
  await setup(page, { detailsExpired: true });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await pickAndStart(page, { done: 'checkup-expired' });

  // 说清是哪条期限，而不是一句"数据过期"。
  await expect(page.getByTestId('checkup-expired-notice'))
    .toContainText('开始记录保留期之前');
  await expect(page.getByTestId('checkup-expired-notice')).toContainText('不再提供');
  // 到期之后仍然看得到结论与关键数字，并且被告知没有重算。
  await expect(page.getByTestId('checkup-retained-revision')).toContainText('第 5 版');
  await expect(page.getByTestId('checkup-retained-facts')).toContainText('检索到的设施：3 处');
  await expect(page.getByTestId('checkup-retained-note')).toContainText('未重算');

  // 明细一个都不在：设施清单、地图图层、报告抽屉都不该出现。
  await expect(page.getByTestId('checkup-nearest')).toHaveCount(0);
  await expect(page.getByTestId('checkup-query-coverage')).toHaveCount(0);
  // 图上不该有设施标记（"N 个点"那种）。中心标记仍在，它不是明细 —— 地图总得有一个中心。
  const drawn = await audit(page);
  expect(drawn.markers.map(marker => marker.options.title)
    .filter(title => /^\d+ 个点/.test(title))).toEqual([]);
  expect(drawn.paths).toHaveLength(0);

  // 到期不是可重试错误：没有"继续检索缺口"，也不该冒出一句体检失败。
  await expect(page.getByTestId('checkup-retry-run')).toHaveCount(0);
  expect(errors).toEqual([]);
});
