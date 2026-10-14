import { defineConfig } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { analysisPython, offlineBrowser } from './tests/browser';

/**
 * 离线集成：真后端、真预算账本、合成上游、离线地图 SDK。
 *
 * 与 `playwright.checkup.config.ts` 的分工是**挡不挡接口**：
 *
 * - checkup-ui 把 `/api/v2/*` 全部挡下来，测"后端这样报，界面就那样画"；
 * - 这一套一次都不挡，后端是 `checkup_browser_app.py`（生产代码 + 合成替身），
 *   于是派发数、账本增量、缓存复用与幂等语义都是真的，浏览器里看到的是真结果。
 *
 * 后端进程里有套接字守卫，用例最后要求外部连接次数为 0：这一轮不花任何百度额度，
 * 也不需要任何真实 AK（`Settings(_env_file=None, …)` 根本不读 backend/.env）。
 */
const artifacts = process.env.INTEGRATION_OUTPUT_DIR || 'output/checkup-integration';
const temp = resolve(artifacts, 'tmp');
mkdirSync(temp, { recursive: true });
process.env.TEMP = temp;
process.env.TMP = temp;
const python = analysisPython();

const API_PORT = Number(process.env.CHECKUP_BROWSER_API_PORT ?? 8022);
const WEB_PORT = Number(process.env.CHECKUP_BROWSER_WEB_PORT ?? 5184);
const data = resolve(process.env.CHECKUP_BROWSER_DIR ?? '../.tmp/checkup-browser');

export default defineConfig({
  testDir: './tests', testMatch: 'checkup-browser.spec.ts', fullyParallel: false, workers: 1,
  timeout: 240000, expect: { timeout: 10000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: resolve(artifacts, 'report') }]],
  outputDir: resolve(artifacts, 'results'),
  use: { baseURL: `http://127.0.0.1:${WEB_PORT}`, ...offlineBrowser(),
    viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: [
    { command: `"${python}" -m uvicorn checkup_browser_app:app --app-dir tests`
        + ` --host 127.0.0.1 --port ${API_PORT} --no-access-log`,
      cwd: '../backend', url: `http://127.0.0.1:${API_PORT}/control/state`, reuseExistingServer: false,
      timeout: 120000, stdout: 'ignore', stderr: 'pipe',
      env: { PYTHONPATH: resolve('../backend'), PYTHONPYCACHEPREFIX: resolve(temp, 'pycache'),
        BROWSER_DIR: data, BROWSER_ORIGIN: `http://127.0.0.1:${WEB_PORT}` } },
    { command: `npm run dev -- --port ${WEB_PORT} --strictPort`, url: `http://127.0.0.1:${WEB_PORT}`,
      reuseExistingServer: false, timeout: 120000, stdout: 'ignore', stderr: 'pipe',
      env: { VITE_ANALYSIS_MODE: 'checkup', VITE_API_BASE_URL: `http://127.0.0.1:${API_PORT}`,
        VITE_BAIDU_MAP_AK: 'offline-sdk-fixture' } },
  ],
});
