/** Capture an existing private result; never creates or continues a checkup. */
import { chromium } from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

if (!process.env.CHECKUP_REVIEW_RESULT || !process.env.CHECKUP_REVIEW_SESSION) {
  throw new Error('CHECKUP_REVIEW_RESULT and CHECKUP_REVIEW_SESSION must reference private saved JSON files');
}
const result = JSON.parse(readFileSync(process.env.CHECKUP_REVIEW_RESULT, 'utf8'));
const session = JSON.parse(readFileSync(process.env.CHECKUP_REVIEW_SESSION, 'utf8'));
const engine = result.engine.engineId;
const output = resolve(process.env.CHECKUP_REVIEW_OUTPUT ?? 'output/submission-review');
mkdirSync(output, { recursive: true });
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true });
const errors = [];
let refusedMutations = 0;
try {
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  await context.addInitScript(({ result, session, engine }) => {
    localStorage.setItem('life-circle:checkup:v1:session', session.sessionId);
    // Open a distinct browser tab lease; closing a screenshot must not close the collector's lease.
    localStorage.setItem('life-circle:checkup:v1:' + engine, JSON.stringify({
      handle: { input: { center: result.center, engine, budget: 200,
        clientRequestId: 'read-only-submission-review' }, taskId: result.taskId, savedAt: Date.now() },
      prefs: { draft: result.center, budget: 200, reportOpen: true, tab: 'layers' },
    }));
  }, { result: { center: result.center, taskId: result.taskId }, session, engine });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/v2/checkups**', route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET' && !path.includes('/sessions')) {
      refusedMutations++;
      return route.abort();
    }
    return route.continue();
  });
  await page.goto((process.env.CHECKUP_REVIEW_URL || 'http://127.0.0.1:5173/')
    + (engine === 'osm_hybrid' ? '#/checkup/hybrid' : '#/checkup/e82'));
  await page.getByTestId('essential-report').waitFor({ state: 'visible', timeout: 90000 });
  await page.getByTestId('essential-report').scrollIntoViewIfNeeded();
  await page.screenshot({ path: resolve(output, 'essential-report-desktop.png') });
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !!window.BMapGL, undefined, { timeout: 60000 });
  await page.getByTestId('checkup-map').waitFor({ state: 'visible' });
  await page.screenshot({ path: resolve(output, 'essential-map-desktop.png') });
  await page.getByRole('button', { name: '查看体检报告', exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByTestId('essential-report').scrollIntoViewIfNeeded();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  await page.screenshot({ path: resolve(output, 'essential-report-narrow.png') });
  writeFileSync(resolve(output, 'capture-summary.json'), JSON.stringify({ taskId: result.taskId,
    revision: result.revision, resultHash: result.trace.resultHash, errors, overflow, refusedMutations }, null, 2));
  if (errors.length || overflow || refusedMutations) throw new Error('Read-only capture validation failed');
} finally {
  await browser.close();
}
