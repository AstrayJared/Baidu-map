/** Explicit live acceptance: click N manual rounds of an EXISTING report.
 * No new boundary tasks or detail-route requests are allowed by this harness.
 */
import { chromium, expect } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const [taskId, count = '2'] = process.argv.slice(2);
if (!taskId || !/^\d+$/.test(count) || +count < 1 || +count > 10) throw new Error('Expected task ID and 1–10 manual rounds');
const output = resolve(process.env.CHECKUP_CONTINUATION_OUTPUT ?? 'output/checkup-continuation-live');
mkdirSync(output, { recursive: true });
const api = `http://127.0.0.1:8000/api/v2/checkups/${encodeURIComponent(taskId)}`;
async function read(suffix = '') {
  const response = await fetch(api + suffix);
  if (!response.ok) throw new Error(`Read failed: ${response.status}`);
  return response.json();
}
const initialTask = await read();
const initial = await read('/result');
writeFileSync(resolve(output, 'before.json'), JSON.stringify(initial, null, 2));
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const rounds = [];
try {
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  await context.addInitScript(({ initial, initialTask }) => {
    const key = `life-circle:checkup:v1:${initialTask.engine}`;
    if (!localStorage.getItem(key)) localStorage.setItem(key, JSON.stringify({
      handle: { input: { center: initial.center, engine: initialTask.engine,
        budget: initialTask.budget, clientRequestId: initialTask.clientRequestId },
        taskId: initialTask.taskId, savedAt: Date.now() },
      prefs: { draft: initial.center, budget: initialTask.budget, reportOpen: false, tab: 'layers' },
    }));
  }, { initial, initialTask });
  const page = await context.newPage();
  let posts = 0;
  await page.route('**/api/v2/**', async route => {
    const request = route.request();
    if (request.method() === 'POST') {
      if (new URL(request.url()).pathname !== `/api/v2/checkups/${taskId}/continue` || posts >= +count) {
        await route.abort();
        throw new Error('Unexpected paid action during continuation acceptance');
      }
      posts++;
    }
    await route.continue();
  });
  await page.goto('http://127.0.0.1:5173/');
  await expect(page.getByTestId('checkup-phase')).toHaveAttribute('data-phase', 'completed', { timeout: 60000 });
  for (let index = 0; index < +count; index++) {
    const before = await read();
    if (!before.completion.canContinue) break;
    const close = page.locator('.ant-drawer-close');
    if (await close.isVisible()) await close.click();
    const reply = page.waitForResponse(r => r.url() === api + '/continue' && r.request().method() === 'POST');
    await page.getByTestId('checkup-continue').click({ timeout: 30000 });
    const response = await reply;
    if (response.status() !== 202) throw new Error(`Continuation refused: ${response.status()} ${await response.text()}`);
    const deadline = Date.now() + 1800000;
    let lastStage = '', lastLog = 0;
    while (true) {
      const status = await read();
      if (status.stage !== lastStage || Date.now() - lastLog > 60000) {
        console.log(JSON.stringify({ round: status.completion.roundNumber, stage: status.stage,
          poi: status.completion.roundPoiRequests, cumulative: status.completion.cumulativePoiRequests }));
        lastStage = status.stage; lastLog = Date.now();
      }
      if (['completed', 'failed', 'cancelled'].includes(status.status)) {
        if (status.status !== 'completed') throw new Error(`Round stopped: ${status.error ?? status.status}`);
        rounds.push(status);
        break;
      }
      if (Date.now() > deadline) throw new Error('Round deadline exceeded');
      await new Promise(resolve => setTimeout(resolve, 3000));
    }
    await expect(page.getByTestId('checkup-phase')).toHaveAttribute('data-phase', 'completed', { timeout: 60000 });
    const snapshot = await read('/result');
    if (JSON.stringify(snapshot.isochrone) !== JSON.stringify(initial.isochrone)) throw new Error('Boundary changed');
    if (snapshot.completion.roundPoiRequests > snapshot.completion.roundPoiLimit || snapshot.completion.routeRequests > 120) throw new Error('Budget exceeded');
    writeFileSync(resolve(output, `round-${snapshot.completion.roundNumber}.json`), JSON.stringify(snapshot, null, 2));
  }
  await page.reload();
  await expect(page.getByTestId('checkup-phase')).toHaveAttribute('data-phase', 'completed', { timeout: 60000 });
  const close = page.locator('.ant-drawer-close');
  if (await close.isVisible()) await close.click();
  await page.screenshot({ path: resolve(output, 'map-and-progress.png') });
  await page.getByRole('button', { name: '查看体检报告', exact: true }).click();
  const report = page.getByTestId('checkup-report');
  await expect(report).toBeVisible();
  await expect(report.locator('li[data-testid^="coverage-"]')).toHaveCount(10);
  await page.screenshot({ path: resolve(output, 'report-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: resolve(output, 'report-narrow.png') });
  const final = await read('/result');
  writeFileSync(resolve(output, 'acceptance.json'), JSON.stringify({ taskId, posts,
    initialRevision: initial.revision, finalRevision: final.revision,
    boundaryUnchanged: true, completion: final.completion, rounds }, null, 2));
  console.log(JSON.stringify({ passed: true, posts, finalRevision: final.revision, completion: final.completion }));
} finally {
  await browser.close();
}
