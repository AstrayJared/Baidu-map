/** Read-only screenshots of an already completed checkup. Never submits a task. */
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

const taskId = process.env.CHECKUP_REVIEW_TASK_ID;
const clientRequestId = process.env.CHECKUP_REVIEW_REQUEST_ID;
if (!taskId || !clientRequestId) throw new Error('A saved task and request ID are required');
const output = resolve(process.env.CHECKUP_REVIEW_OUTPUT ?? 'output/checkup-review');
mkdirSync(output, { recursive: true });
const center = { lng: 121.513925, lat: 31.313079 };
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  await context.addInitScript(({ taskId, clientRequestId, center }) => {
    localStorage.setItem('life-circle:checkup:v1:baidu_e82', JSON.stringify({
      handle: { input: { center, engine: 'baidu_e82', budget: 400, clientRequestId },
        taskId, savedAt: Date.now() },
      prefs: { draft: center, budget: 400, reportOpen: true, tab: 'layers' },
    }));
  }, { taskId, clientRequestId, center });
  const page = await context.newPage();
  await page.route('**/api/v2/checkups', route => {
    if (route.request().method() === 'POST') throw new Error('Read-only review attempted task creation');
    return route.continue();
  });
  await page.goto('http://127.0.0.1:5173/');
  const report = page.getByTestId('checkup-report');
  await report.waitFor({ state: 'visible', timeout: 60000 });
  await report.getByTestId('verification-summary').waitFor({ timeout: 30000 });
  await page.getByRole('navigation', { name: '报告目录' })
    .getByRole('button', { name: '分类覆盖' }).click();
  await report.getByTestId('coverage-transport').scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(output, 'report-categories-desktop.png') });
  await page.getByRole('navigation', { name: '报告目录' })
    .getByRole('button', { name: '现实核验' }).click();
  await report.getByTestId('verification-summary').scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(output, 'report-verification-desktop.png') });
  await report.getByText('逐设施路线证据').click();
  await report.getByText('逐设施路线证据').scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(output, 'report-evidence-detail-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('navigation', { name: '报告目录' })
    .getByRole('button', { name: '分类覆盖' }).click();
  await report.getByTestId('coverage-transport').scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(output, 'report-categories-narrow.png') });
} finally {
  await browser.close();
}
