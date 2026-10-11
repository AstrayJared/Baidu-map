/**
 * 真实验收跑（§11.4）：真 AK、真 BMapGL 底图、真后端、真百度接口。
 *
 * 这一套**不挡任何请求**，与 `checkup-ui.spec.ts` 正好相反 —— 那套证明界面接线对，
 * 这套证明整条链路真的跑得通，而且跑出来的东西是能归档的证据：
 *
 * - 任务号、修订、`resultHash`、报告 JSON 全部按接口原文落盘，不在这里重新表述；
 * - 截图必须是真底图：所以先断言地图没降级（没有"地图不可用"提示、容器里有画布），
 *   再截图。真 AK 加载失败时页面仍然能跑完体检，只是地图是空的 —— 那种截图看着像通过。
 * - 覆盖区间、覆盖面积、灰区面积、热力采样四项要出现在**同一轮**里，缺一项这轮就不算数。
 *
 * 它会花真实额度，因此不进任何常规套件，也不并发。跑之前后端要在 8000 端口上。
 */
import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { LAYER_IDS, type LayerId } from '../src/checkup/validate';

const API = 'http://127.0.0.1:8000';
const OUTPUT = resolve(process.env.CHECKUP_LIVE_OUTPUT_DIR ?? 'output/checkup-live');
const redact = (value: string) => value.replace(/([?&](?:ak|key|token)=)[^&\s]+/gi, '$1[REDACTED]');
/** 国定一社区。历次真实跑用的同一个中心，报告之间才可比。 */
const CENTER = { lng: 121.513925, lat: 31.313079 };
/** 档位与引擎可以按轮次收窄（例如只跑 E8.2.1 的 800 档），默认仍是两个引擎各 400。 */
const BUDGET = Number(process.env.CHECKUP_LIVE_BUDGET ?? 400);
const ALL_ENGINES: Record<string, { label: string; version: string }> = {
  baidu_e82: { label: '百度边界搜索（E8.2）', version: 'local-multicross-e82' },
  osm_hybrid: { label: 'OSM＋百度', version: 'hybrid-v1.5.0' },
};
const ENGINES = Object.fromEntries(Object.entries(ALL_ENGINES).filter(([id]) =>
  !process.env.CHECKUP_LIVE_ENGINES || process.env.CHECKUP_LIVE_ENGINES.split(',').includes(id)));
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
/** 任务的截止时间是后端自己的 1800 秒，这里留出取图层、截图与写盘的时间。 */
const TASK_TIMEOUT_MS = 1680000;

type TaskView = {
  taskId: string; status: string; stage: string | null; revision: number;
  businessStatus: string | null; engine: string; budget: number; networkRequests: number;
  elapsedSeconds: number; error: string | null;
};
type Layer = { layerId: string; revision: number; resultHash: string; document: unknown };

/**
 * 取一次 JSON，容忍连接被重置。
 *
 * 轮询一次体检要好几分钟，而 uvicorn 会关掉空闲的长连接（默认 5 秒），这里恰好也是几秒一次。
 * 于是复用池里的 socket 会时不时在半路被对端关掉，`read ECONNRESET` —— 那是**这一问**失败，
 * 不是任务的结论。把它当结论会让"跑了 6 分钟的真实体检"以一句网络错误收场。
 */
async function fetchJson(request: APIRequestContext, url: string): Promise<unknown> {
  for (let attempt = 0; ; attempt++) {
    try {
      const response = await request.get(url, { headers: { connection: 'close' }, timeout: 30000 });
      return await response.json();
    } catch (error) {
      if (attempt >= 5) throw error;
      await new Promise(ready => setTimeout(ready, 2000));
    }
  }
}

/** 按任务号轮询到终态。失败状态也返回：交给断言去说，别在这里变成一个超时。 */
async function waitForTask(request: APIRequestContext, taskId: string): Promise<TaskView> {
  const deadline = Date.now() + TASK_TIMEOUT_MS;
  for (;;) {
    const view = await fetchJson(request, `${API}/api/v2/checkups/${taskId}`) as TaskView;
    if (TERMINAL.has(view.status)) return view;
    if (Date.now() > deadline) {
      throw new Error(`任务 ${taskId} 在 ${TASK_TIMEOUT_MS / 1000} 秒内没有结束，`
        + `最后状态 ${view.status}／${view.stage}`);
    }
    await new Promise(ready => setTimeout(ready, 5000));
  }
}

/** 真底图在场。脚本加载失败时页面照样能跑完体检，只是地图是空的。 */
async function expectRealBasemap(page: Page) {
  await expect(page.getByText('地图不可用')).toHaveCount(0);
  await expect(page.getByText('正在加载百度地图')).toHaveCount(0);
  await expect.poll(async () => page.getByTestId('checkup-map').locator('canvas:not([data-map-layer])').count(),
    { timeout: 60000 }).toBeGreaterThan(0);
  // 画布在，瓦片还没贴上去：BMapGL 是 WebGL 贴图，既不进 resource 列表也没有事件，
  // 只能给它一点时间。少了这一步，截出来的底图是全白的，而断言全都是通过的。
  await page.waitForTimeout(9000);
}

test('浏览器 AK 能加载真实 BMapGL 底图，页面也能连上真后端', async ({ page }) => {
  // 排在最前：这个不花额度，却决定了后面两张截图有没有意义。AK 被 Referer 白名单
  // 挡住时，页面仍会跑完体检并"成功"，只有地图是空的；后端连不上时反过来 ——
  // 地图好好的，但一条体检也提交不出去。
  const problems: string[] = [];
  page.on('pageerror', error => problems.push(redact(`page: ${error.message}`)));
  page.on('console', message => {
    if (message.type() === 'error') problems.push(redact(`console: ${message.text()}`));
  });
  page.on('requestfailed', request =>
    problems.push(redact(`${request.url()} :: ${request.failure()?.errorText}`)));
  await page.goto('/');
  await expectRealBasemap(page).catch(error => {
    throw new Error(`真实底图预检失败：${problems.join(' | ') || '没有浏览器错误记录'}\n${error}`);
  });
  expect(await page.evaluate(() => typeof (window as { BMapGL?: unknown }).BMapGL)).toBe('object');
  // 能力表来自真后端：预算档位能选，就说明这一页确实读到了它（档位只来自能力表）。
  await page.getByTestId('checkup-tab-engine').click();
  await expect(page.getByRole('combobox', { name: '调用预算' }))
    .toBeEnabled({ timeout: 60000 }).catch(error => {
      throw new Error(`读不到 /api/v2/capabilities：${problems.join(' | ') || '没有错误记录'}\n${error}`);
    });
  await expect(page.getByTestId('quota-label'))
    .toHaveText('本应用请求限制（不含浏览器 SDK、其他应用及旧接口流量）');
  await page.locator('.api-map-shell').screenshot({
    path: resolve(OUTPUT, 'basemap.png') });
});

/**
 * 在 antd 的 Select 里选一项。
 *
 * 不能用 `getByRole('option')`：下拉里有两份 option —— 看得见的那份在
 * `.ant-select-dropdown` 里，另一份是给读屏器的隐藏列表（文本是**取值**而非标签，
 * 所以它才有 aria-label）。按 role 点会点到隐藏那份，于是永远"不可见"，
 * 一条 30 秒的超时看起来像"下拉没打开"。
 */
async function pick(page: Page, combo: string, item: string) {
  await page.getByRole('combobox', { name: combo }).click();
  await page.locator('.ant-select-dropdown:visible .ant-select-item-option')
    .filter({ hasText: item }).first().click();
}

for (const [engine, { label, version }] of Object.entries(ENGINES)) {
  test(`${label}：一次真实体检，从选点到报告`, async ({ page, request }) => {
    const dir = resolve(OUTPUT, engine);
    mkdirSync(dir, { recursive: true });
    await page.goto('/');

    // 引擎在「采样与引擎」选项卡里切换，档位来自后端能力表：界面不自己编档位，这里也不替它编。
    await page.getByRole('spinbutton', { name: '经度', exact: true }).fill(String(CENTER.lng));
    await page.getByRole('spinbutton', { name: '纬度', exact: true }).fill(String(CENTER.lat));
    await page.getByTestId('checkup-tab-engine').click();
    await page.getByRole('radio', { name: engine === 'baidu_e82' ? '百度边界搜索' : '百度 + OSM', exact: true }).check();
    await expect(page.getByRole('combobox', { name: '调用预算' })).toBeEnabled({ timeout: 60000 });
    // 选中的证据用面板上的引擎版本，而不是控件内部的类名：换了引擎版本就该跟着变。
    await expect(page.getByTestId('checkup-engine')).toContainText(label);
    await expect(page.getByTestId('checkup-engine')).toContainText(version);
    await pick(page, '调用预算', String(BUDGET));

    const created = page.waitForResponse(response =>
      response.url().endsWith('/api/v2/checkups') && response.request().method() === 'POST');
    await page.getByRole('button', { name: '开始体检', exact: true }).click();
    const submitted = await (await created).json() as TaskView;
    // 后端接下的这两项就是这一轮真正要跑的：档位是引擎自己给的（§4.1 不支持的档一律 422），
    // 所以它比下拉框里显示的字更值得断言。
    expect(submitted.engine, '后端接下的引擎必须就是选的那个').toBe(engine);
    expect(submitted.budget).toBe(BUDGET);

    const finished = await waitForTask(request, submitted.taskId);
    expect(finished.status, `任务 ${finished.taskId} 以 ${finished.status} 结束：`
      + `${finished.error ?? ''}`).toBe('completed');
    expect(finished.revision).toBeGreaterThan(0);

    // 一次取全：快照、报告、六个几何图层。报告是文档，与图层同源同修订。
    const root = `${API}/api/v2/checkups/${finished.taskId}`;
    const snapshot = await fetchJson(request, `${root}/result`);
    const report = await fetchJson(request, `${root}/layers/report`) as Layer;
    const layers: { id: string; status: number; body: unknown }[] = [];
    for (const id of LAYER_IDS.filter(item => item !== 'report') as LayerId[]) {
      const response = await request.get(`${root}/layers/${id}`,
        { headers: { connection: 'close' } });
      layers.push({ id, status: response.status(), body: await response.json() });
    }
    writeFileSync(resolve(dir, 'task.json'), JSON.stringify(finished, null, 2));
    writeFileSync(resolve(dir, 'snapshot.json'), JSON.stringify(snapshot, null, 2));
    writeFileSync(resolve(dir, 'report.json'), JSON.stringify(report, null, 2));
    const frozen = snapshot as { facilities?: { statistics?: Record<string, unknown> };
      report?: { categoryDirectory?: unknown[] } };
    writeFileSync(resolve(dir, 'category-call-distribution.json'), JSON.stringify({
      requestsByMajor: frozen.facilities?.statistics?.requestsByMajor ?? null,
      touchedBlocksByMajor: frozen.facilities?.statistics?.touchedBlocksByMajor ?? null,
      unfinishedByMajor: frozen.facilities?.statistics?.unfinishedByMajor ?? null,
      categoryDirectory: frozen.report?.categoryDirectory ?? null,
    }, null, 2));
    for (const item of layers) {
      writeFileSync(resolve(dir, `layer-${item.id}.json`), JSON.stringify(item.body, null, 2));
    }
    // 每个几何图层都必须拿得到：409 是"这一层没做"，不是"这一层是空的"。
    expect(layers.filter(item => item.status !== 200).map(item => `${item.id}=${item.status}`))
      .toEqual([]);
    expect(report.resultHash, '报告图层应当带着这一版的结果指纹').toBeTruthy();
    expect(report.revision).toBe(finished.revision);
    // checked/failed 计数不能证明路线返回过；必须检查实际端点与距离。
    const verification = (snapshot as { verification?: { facilities?: {
      routeDistanceM?: number | null; durationS?: number | null;
      routeOrigin?: unknown; routeDestination?: unknown; poiStatus?: string;
      verificationLayer?: string | null;
    }[] } }).verification?.facilities ?? [];
    const roadEvidence = verification.filter(row => typeof row.routeDistanceM === 'number'
      && Number.isFinite(row.routeDistanceM) && typeof row.durationS === 'number'
      && row.routeOrigin != null && row.routeDestination != null);
    writeFileSync(resolve(dir, 'verification-audit.json'), JSON.stringify({
      returnedRoutes: roadEvidence.length,
      strictConfirmed: verification.filter(row => row.verificationLayer === 'strict').length,
      toleranceEstimated: verification.filter(row => row.verificationLayer === 'endpoint_tolerance').length,
      noUsableDecision: verification.filter(row => row.verificationLayer == null).length,
      pendingStrictEndpoint: verification.filter(row => row.poiStatus === 'pending').length,
    }, null, 2));

    // 界面上必须真的报告了这一版：抽屉在完成时自动打开，指纹与修订都要对得上。
    const drawn = page.getByTestId('checkup-report');
    await expect(drawn).toBeVisible({ timeout: 120000 });
    await expect(drawn).toContainText('15 分钟生活圈体检报告');
    await expect(drawn).toContainText(`${finished.taskId} · 第 ${finished.revision} 版`);
    await expect(drawn).toContainText(report.resultHash);

    // Partial categories remain visible as unavailable; this run does not require full search completion.
    const text = await drawn.innerText();
    expect(text).toContain('分类覆盖');
    expect(await page.locator('li[data-testid^="coverage-"]').count()).toBe(10);

    await page.screenshot({ path: resolve(dir, 'report-top.png') });
    await page.keyboard.press('Escape');
    await expect(drawn).toBeHidden();

    // 真底图 + 真图层。要的就是"这不是示意地图"：默认的服务覆盖热力先截一张，
    // 再换成设施密度截一张。图层开关在「图层备注」选项卡里。
    await expectRealBasemap(page);
    await page.getByTestId('checkup-tab-layers').click();
    // 只数业务 Canvas 上画了多少像素，不能把百度底图或图表 Canvas 当作热力。
    await expect(page.getByRole('checkbox', { name: '服务覆盖热力', exact: true })).toBeChecked();
    await expect(page.getByTestId('service-heat-canvas')).toBeVisible();
    await expect(page.getByTestId('service-legend')).toContainText('模型估计');
    await page.locator('.api-map-shell').screenshot({ path: resolve(dir, 'map.png') });
    await page.screenshot({ path: resolve(dir, 'page.png') });

    await page.getByRole('checkbox', { name: '设施密度热力', exact: true }).check();
    await expect(page.getByTestId('service-heat-canvas')).toHaveCount(0);
    await expect(page.getByTestId('facility-density-canvas')).toBeVisible();
    // The map may have no drawable density in a budget-limited category; the capture remains evidence.
    await page.locator('.api-map-shell').screenshot({ path: resolve(dir, 'map-density.png') });
  });
}
