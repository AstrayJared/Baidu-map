/**
 * 离线集成：真后端、真预算账本、合成上游，走真实浏览器。
 *
 * 与 `checkup-ui.spec.ts` 的分工是**挡不挡接口**：那一套把 `/api/v2/*` 全部挡下来，
 * 测的是"后端这样报，界面就那样画"；这一套一次都不挡，后端的派发数、日账本、缓存复用
 * 与幂等语义都是真的。所以这里的每一个数字都能追到一次原子的额度预留。
 *
 * 三条纪律：
 *
 * - 上游与地图都是替身（`checkup_browser_app.py`），进程里有套接字守卫；
 *   最后断言 `external.attempts == 0`——这一轮一次真实额度都没花。
 * - 派发数一律读 `/control/state` 的**前后差值**，并要求它与日账本增量相等：
 *   "处理了多少页"（含缓存重放）和"真正发出多少次"是两本账。
 * - 界面上说得对不对也要断言：上游一直失败时，界面**不许**把"没取到"说成"没有设施"。
 */
import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { EXTENSION_STOP_LABELS } from '../src/checkup/extensions';
import { installMapSdk } from './mapSdk';

const API = `http://127.0.0.1:${process.env.CHECKUP_BROWSER_API_PORT ?? 8022}`;
/** 地图选点固定回这个坐标（见 mapSdk），也就是合成设施所在的任务中心。 */
const CENTRE = { lng: 116.405, lat: 39.916 };
const CORE = ['shopping', 'medical', 'education'];
const DONE = new Set(['completed', 'failed', 'cancelled']);
/** 补查的终态多一个 `partial`：补查不发布新修订，停在部分结果就是终态。 */
const EXTENSION_DONE = new Set(['completed', 'partial', 'failed', 'cancelled']);

type ControlState = {
  mode: string; dispatched: number; routes: number; cachePages: number;
  ledger: { day: string; spent: number; budget: number; remaining: number };
  external: { attempts: number; targets: string[] };
  tasks: { taskId: string; clientRequestId: string; status: string; revision: number }[];
};
type TaskView = { taskId: string; status: string; stage: string | null; revision: number;
  facilitiesStatus: string | null };
type FacilityGroup = {
  queryStatus: string; stopReason: string | null; facilities: unknown[];
  countsByCategory: Record<string, number>; statistics: Record<string, number>;
  initialPlan: { initialPageCount: number; reusableInitialPageCount: number } | null;
};
type TaskDocument = { facilitiesStatus: string | null; facilities: FacilityGroup | null;
  warnings: { code: string; message: string; scope: string }[] };
type ExtensionView = { extensionId: string; status: string; baseRevision: number;
  requests: number; networkRequests: number; stopReason: string | null };

const getState = async (request: APIRequestContext): Promise<ControlState> => {
  const response = await request.get(`${API}/control/state`);
  expect(response.ok()).toBe(true);
  return await response.json() as ControlState;
};
const post = async (request: APIRequestContext, path: string, data: unknown = {}): Promise<void> => {
  const response = await request.post(`${API}${path}`, { data });
  expect(response.ok(), `POST ${path} → ${response.status()} ${await response.text()}`).toBe(true);
};
const document_ = async (request: APIRequestContext, taskId: string): Promise<TaskDocument> =>
  await (await request.get(`${API}/api/v2/checkups/${taskId}/result`)).json() as TaskDocument;

const checkupBody = (clientRequestId: string, maxPoiRequests = 60) => ({
  schemaVersion: 'checkup-v1', clientRequestId, engine: 'baidu_e82', center: CENTRE,
  coordinateSystem: 'bd09ll', isochrone: { budget: 200 },
  facilities: { categories: CORE, maxPoiRequests },
});

async function settle(request: APIRequestContext, taskId: string, timeout = 120000): Promise<TaskView> {
  const deadline = Date.now() + timeout;
  let view = await (await request.get(`${API}/api/v2/checkups/${taskId}`)).json() as TaskView;
  while (!DONE.has(view.status)) {
    if (Date.now() > deadline) {
      const state = await (await request.get(`${API}/control/state`)).json();
      await test.info().attach('task-timeout-evidence', {
        body: JSON.stringify({ task: view, state }, null, 2), contentType: 'application/json',
      });
      throw new Error(`task ${taskId} did not settle: ${view.status}, stage=${view.stage}, progress=${JSON.stringify(view.progress)}`);
    }
    await new Promise(resolve => setTimeout(resolve, 100));
    view = await (await request.get(`${API}/api/v2/checkups/${taskId}`)).json() as TaskView;
  }
  return view;
}

async function settleExtension(request: APIRequestContext, taskId: string,
                               extensionId: string): Promise<ExtensionView> {
  const deadline = Date.now() + 120000;
  const at = `${API}/api/v2/checkups/${taskId}/facility-extensions/${extensionId}`;
  let view = await (await request.get(at)).json() as ExtensionView;
  while (!EXTENSION_DONE.has(view.status)) {
    if (Date.now() > deadline) throw new Error(`extension ${extensionId} did not settle: ${view.status}`);
    await new Promise(resolve => setTimeout(resolve, 100));
    view = await (await request.get(at)).json() as ExtensionView;
  }
  return view;
}

async function extensionGroup(request: APIRequestContext, taskId: string,
                              extensionId: string): Promise<FacilityGroup> {
  const response = await request.get(
    `${API}/api/v2/checkups/${taskId}/facility-extensions/${extensionId}/result`);
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json() as { group: FacilityGroup }).group;
}

const serve = async (page: Page) => {
  await installMapSdk(page);
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    // 天气卡片的数据源：给一份固定实况，别让用例依赖外网（这一条也不出进程）。
    if (url.hostname === 'api.open-meteo.com') {
      return route.fulfill({ json: { current: { time: '2026-10-08T12:00', temperature_2m: 21.5,
        relative_humidity_2m: 55, apparent_temperature: 22.1, weather_code: 1, wind_speed_10m: 9.2 } } });
    }
    if (url.hostname !== '127.0.0.1') return route.abort();
    return route.continue();
  });
  await page.goto('/');
};

/** 页面真的创建了任务：在服务端的任务表里认出新出现的那一个，只认一个。 */
async function waitForNewTask(request: APIRequestContext, before: ControlState,
                              timeout = 90000): Promise<string> {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const now = await getState(request);
    const created = now.tasks.filter(task => !before.tasks.some(old => old.taskId === task.taskId));
    if (created.length > 0) {
      expect(created.length, '一次提交只应创建一个任务').toBe(1);
      return created[0].taskId;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('页面没有创建体检任务');
}

/** 点地图选点、开始体检；返回这一次任务的服务端标识。 */
async function startCheckup(page: Page, request: APIRequestContext, before: ControlState, maxPoiRequests?: number): Promise<string> {
  if (maxPoiRequests !== undefined) await page.route('**/api/v2/checkups', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const body = route.request().postDataJSON();
    return route.continue({ postData: JSON.stringify({ ...body,
      facilities: { ...body.facilities, maxPoiRequests } }) });
  });
  await page.getByTestId('checkup-map').click();
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  const taskId = await waitForNewTask(request, before);
  await settle(request, taskId, 180000);
  await expect(page.getByTestId('checkup-report')).toBeVisible();
  return taskId;
}

/** 报告正文。收起的分组用 `textContent` 读：`details` 收起时内容仍在 DOM 里。 */
const reportText = async (page: Page): Promise<string> =>
  await page.getByTestId('checkup-report').textContent() ?? '';
const nearestText = (page: Page) => page.getByTestId('checkup-nearest').innerText();

test.beforeEach(async ({ request }) => {
  await post(request, '/control/reset');
});

test('一次体检把设施画到图上，派发数与账本增量逐页对得上', async ({ page, request }) => {
  await post(request, '/control/allowance', { placeDailyBudget: 1600 });
  const before = await getState(request);
  await serve(page);
  const taskId = await startCheckup(page, request, before);
  const doc = await document_(request, taskId);
  const after = await getState(request);
  const group = doc.facilities!;

  expect(doc.facilitiesStatus).toBe('complete');
  expect(group.queryStatus).toBe('completed');
  // 首轮估算就是这一轮真正派发的页数：4 个查询分块 × 14 个检索小类，一页都不多。
  expect(after.dispatched - before.dispatched).toBe(group.initialPlan!.initialPageCount);
  expect(after.ledger.spent - before.ledger.spent).toBe(group.initialPlan!.initialPageCount);
  expect(after.dispatched).toBe(after.ledger.spent);
  // 设施真的落到了界面上：最近设施列表按类别列出，不是"尚未加载"。
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('checkup-nearest')).toBeVisible();
  expect(await page.getByTestId('checkup-nearest').locator('button').count()).toBeGreaterThan(0);
  expect(await nearestText(page)).not.toContain('尚未加载');
  expect(await nearestText(page)).not.toContain('没有接收的设施');
  expect((await getState(request)).external.attempts).toBe(0);
});

test('显式60次预算装不下一整轮时，界面说 partial 并且保留已取到的证据', async ({ page, request }) => {
  await post(request, '/control/allowance', { placeDailyBudget: 1600 });
  await post(request, '/control/respond', { mode: 'dense' });
  const before = await getState(request);
  await serve(page);
  const taskId = await startCheckup(page, request, before, 60);
  const doc = await document_(request, taskId);
  const after = await getState(request);
  const group = doc.facilities!;

  // 首轮 56 页装得进 60，整轮 112 页装不下：额度正好停在 60 次，一次都不越。
  expect(group.queryStatus).toBe('partial');
  expect(group.stopReason).toBe('network_budget_exhausted');
  expect(after.dispatched - before.dispatched).toBe(60);
  expect(after.ledger.spent - before.ledger.spent).toBe(60);
  expect(after.dispatched).toBe(after.ledger.spent);
  expect(doc.facilitiesStatus).toBe('partial');
  // 查不完不等于查不到：已取到的证据必须留着（不是零设施、也没标成 completed）。
  expect(group.facilities.length).toBeGreaterThan(0);
  expect(group.statistics.networkCalls).toBe(60);
  await page.keyboard.press('Escape');
  expect(await nearestText(page)).not.toContain('没有接收的设施');
  expect((await getState(request)).external.attempts).toBe(0);
});

test('日余额为 0 时，补查复用已经付费的页面而不新增一次派发', async ({ page, request }) => {
  await post(request, '/control/allowance', { placeDailyBudget: 1600 });
  await post(request, '/control/respond', { mode: 'dense' });
  const before = await getState(request);
  await serve(page);
  const taskId = await startCheckup(page, request, before, 60);
  await page.keyboard.press('Escape');
  const afterRun = await getState(request);

  await post(request, '/control/allowance', { placeDailyBudget: 0 });
  const created = await request.post(`${API}/api/v2/checkups/${taskId}/facility-extensions`,
    { data: { clientRequestId: 'extension-zero', categories: CORE } });
  expect(created.status(), await created.text()).toBe(202);
  const { extensionId } = await created.json() as { extensionId: string };
  const view = await settleExtension(request, taskId, extensionId);
  const group = await extensionGroup(request, taskId, extensionId);
  const after = await getState(request);

  // 额度为 0，但缓存里的页面是已经付过费的：一次新调用都没有，账本一分不动。
  expect(view.networkRequests).toBe(0);
  expect(after.dispatched).toBe(afterRun.dispatched);
  expect(after.ledger.spent).toBe(afterRun.ledger.spent);
  // 两本账分得开：处理了全部页面，其中一半来自缓存，缺的那些被额度挡在派发之前。
  expect(view.requests).toBeGreaterThan(0);
  expect(group.statistics.cachedPages).toBeGreaterThan(0);
  expect(group.statistics.networkCalls).toBe(0);
  expect(group.statistics.allowanceRefusedPages).toBeGreaterThan(0);
  expect(group.queryStatus).toBe('partial');
  expect(group.stopReason).toBe('network_budget_exhausted');
  expect(group.facilities.length).toBeGreaterThan(0);
  // 刷新后界面把这条补查读回来：结果是服务端的，不靠本地记。
  await page.reload();
  await expect(page.getByTestId('checkup-extensions')).toBeVisible();
  await expect(page.getByTestId('checkup-extensions').locator('li')).toHaveCount(1);
  expect(await page.getByTestId('checkup-extension-facts').first().innerText()).toContain('新增网络 0');
  expect((await getState(request)).external.attempts).toBe(0);
});

test('同一个请求标识是幂等取回，改了参数是冲突，父任务结果一字不变', async ({ request }) => {
  await post(request, '/control/allowance', { placeDailyBudget: 1600 });
  const body = checkupBody('idempotent-1');
  const created = await request.post(`${API}/api/v2/checkups`, { data: body });
  expect(created.status(), await created.text()).toBe(202);
  const { taskId } = await created.json() as { taskId: string };
  await settle(request, taskId);
  const before = await getState(request);
  const original = await document_(request, taskId);

  const replay = await request.post(`${API}/api/v2/checkups`, { data: body });
  expect([200, 202]).toContain(replay.status());
  expect((await replay.json() as { taskId: string }).taskId).toBe(taskId);
  const after = await getState(request);
  expect(after.dispatched).toBe(before.dispatched);
  expect(after.ledger.spent).toBe(before.ledger.spent);
  expect(after.tasks.filter(task => task.clientRequestId === 'idempotent-1').length).toBe(1);
  expect(await document_(request, taskId)).toEqual(original);

  for (const altered of [{ ...body, center: { lng: 121.513925, lat: 31.313079 } },
    checkupBody('idempotent-1', 30)]) {
    const conflict = await request.post(`${API}/api/v2/checkups`, { data: altered });
    expect(conflict.status()).toBe(409);
    expect((await conflict.json() as { code: string }).code).toBe('checkup_request_id_conflict');
  }
  const settled = await getState(request);
  expect(settled.dispatched).toBe(before.dispatched);
  expect(await document_(request, taskId)).toEqual(original);

  // 补查同一个标识同理：取回同一条，改类别就是冲突。
  const extension = { clientRequestId: 'extension-idem', categories: CORE };
  const first = await request.post(`${API}/api/v2/checkups/${taskId}/facility-extensions`,
    { data: extension });
  expect(first.status(), await first.text()).toBe(202);
  const { extensionId } = await first.json() as { extensionId: string };
  const again = await request.post(`${API}/api/v2/checkups/${taskId}/facility-extensions`,
    { data: extension });
  expect([200, 202]).toContain(again.status());
  expect((await again.json() as { extensionId: string }).extensionId).toBe(extensionId);
  const extended = await request.post(`${API}/api/v2/checkups/${taskId}/facility-extensions`,
    { data: { clientRequestId: 'extension-idem', categories: ['dining'] } });
  expect(extended.status()).toBe(409);
  expect((await extended.json() as { code: string }).code)
    .toBe('checkup_extension_request_id_conflict');
  expect(await document_(request, taskId)).toEqual(original);
  expect((await getState(request)).external.attempts).toBe(0);
});

test('上游一直限流时有界停止，界面说出原因而不是说没有设施', async ({ page, request }) => {
  await post(request, '/control/allowance', { placeDailyBudget: 1600 });
  await post(request, '/control/respond', { mode: 'rate_limit' });
  const before = await getState(request);
  await serve(page);
  const taskId = await startCheckup(page, request, before);
  const doc = await document_(request, taskId);
  const after = await getState(request);
  const group = doc.facilities!;

  // 有界：一页、一次重试，两次派发都计费，不会无限重试也不越额。
  expect(group.queryStatus).toBe('failed');
  expect(group.stopReason).toBe('rate_limit');
  expect(group.statistics.networkCalls).toBe(2);
  expect(after.dispatched - before.dispatched).toBe(2);
  expect(after.ledger.spent - before.ledger.spent).toBe(2);
  expect(doc.facilitiesStatus).toBe('failed');
  // 报告照实说：未完全覆盖，并且把上游给的原因原文带出来。
  const report = await reportText(page);
  expect(report).toContain('本次设施检索未完全覆盖');
  // 界面上不许把"没取到"说成"没有设施"。
  await page.keyboard.press('Escape');
  const nearest = await nearestText(page);
  expect(nearest).not.toContain('没有接收的设施');
  expect(nearest).toContain(EXTENSION_STOP_LABELS.rate_limit);
  expect((await getState(request)).external.attempts).toBe(0);
});

test('日余额不足的冷查询被具名拒绝，一次请求都不发', async ({ page, request }) => {
  await post(request, '/control/allowance', { placeDailyBudget: 20 });
  const before = await getState(request);
  await serve(page);
  const taskId = await startCheckup(page, request, before);
  const doc = await document_(request, taskId);
  const after = await getState(request);

  expect(doc.facilitiesStatus).toBe('failed');
  expect(doc.facilities).toBeNull();
  // 拒绝说的是这一次的真实数字，不是一句"不可用"。
  expect(doc.warnings.some(warning => warning.message.includes('本应用今天还剩 20 次'))).toBe(true);
  expect(after.dispatched).toBe(before.dispatched);
  expect(after.ledger.spent).toBe(before.ledger.spent);
  expect(await reportText(page)).toContain('本应用今天还剩 20 次');
  expect((await getState(request)).external.attempts).toBe(0);
});

test('取消在跑的体检：停在已取消，不再发布新修订，已花的额度等于派发数', async ({ page, request }) => {
  await post(request, '/control/allowance', { placeDailyBudget: 1600 });
  // 每页慢下来，才有"正在跑"可取消；否则一轮不到一秒就跑完了。
  await post(request, '/control/respond', { mode: 'dense', delay: 0.2 });
  const before = await getState(request);
  await serve(page);
  await page.getByTestId('checkup-map').click();
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  const taskId = await waitForNewTask(request, before);
  // 等到真的发出去了几笔再取消：要验证的是"已发出去的算钱、没发的不算"。
  await expect.poll(async () => (await getState(request)).dispatched, { timeout: 60000 })
    .toBeGreaterThan(0);
  await expect(page.getByRole('button', { name: '取消任务' })).toBeVisible();
  await page.getByRole('button', { name: '取消任务' }).click();
  const view = await settle(request, taskId);
  const after = await getState(request);

  expect(view.status).toBe('cancelled');
  // 已发出去的请求计费，没有发出的不算：两本账仍然相等，取消不是退款。
  expect(after.dispatched).toBe(after.ledger.spent);
  expect(after.dispatched).toBeGreaterThan(0);
  expect(after.dispatched).toBeLessThan(112);
  // 取消之后不再有新的修订：截图式的"还在跑"不应该出现。
  const revisions = view.revision;
  await page.waitForTimeout(1500);
  const later = await settle(request, taskId);
  expect(later.revision).toBe(revisions);
  await expect(page.getByText('任务已取消')).toBeVisible();
  expect((await getState(request)).external.attempts).toBe(0);
});
