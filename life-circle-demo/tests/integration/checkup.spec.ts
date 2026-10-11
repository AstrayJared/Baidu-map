import { test, expect } from '@playwright/test';
import { installMapSdk } from '../mapSdk';

// Real local API and task store; only the map and walking provider are synthetic.
test('offline checkup reaches an immutable report and restores it after reload', async ({ page }) => {
  await installMapSdk(page);
  const external: string[] = [];
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (['127.0.0.1', 'localhost'].includes(url.hostname)) return route.continue();
    if (url.hostname === 'api.open-meteo.com') {
      return route.fulfill({ json: { current: { temperature_2m: 20, weather_code: 0 } } });
    }
    external.push(url.hostname);
    return route.abort();
  });
  await page.goto('/#/checkup/e82');
  await page.getByTestId('checkup-map').click();
  const created = page.waitForResponse(response => response.url().endsWith('/api/v2/checkups')
    && response.request().method() === 'POST');
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  const response = await created;
  expect(response.status()).toBe(202);
  const submitted = await response.json();
  await expect(page.getByTestId('checkup-report')).toBeVisible({ timeout: 25000 });
  const resultUrl = `http://127.0.0.1:8018/api/v2/checkups/${submitted.taskId}/result`;
  const resultResponse = await page.request.get(resultUrl);
  expect(resultResponse.ok()).toBe(true);
  const report = await resultResponse.json();
  expect(report.taskId).toBe(submitted.taskId);
  expect(report.completion.totalCategories).toBe(10);
  expect(report.completion.evaluationStatus).toBe('partial');
  expect(Object.values(report.completion.queryCompleteByMajor)).toEqual(Array(10).fill(false));
  expect(report.trace.budgets.poi.spent).toBe(0);
  await page.reload();
  await expect(page.getByTestId('checkup-report')).toBeVisible();
  const restored = await page.request.get(`${resultUrl}?revision=${report.revision}`);
  expect(await restored.json()).toEqual(report);
  expect(external).toEqual([]);
});
