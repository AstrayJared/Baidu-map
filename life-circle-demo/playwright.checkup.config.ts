import { defineConfig } from '@playwright/test';
import { resolve } from 'node:path';
import { offlineBrowser } from './tests/browser';

/**
 * v2 体检工作台的界面测试。
 *
 * 与 analysis-ui 分开跑：两套页面读的是不同的契约（`/api/v2/checkups` 与 `/api/analyses`），
 * 端口也必须错开 —— 共用一个端口时，先启动的那个开发服务器会被复用，于是这一套会跑到
 * 旧页面上，全部断言以一种看不懂的方式失败（找不到"开始体检"）。
 */
export default defineConfig({
  testDir: './tests', testMatch: 'checkup-ui.spec.ts', workers: 1,
  timeout: 30000, expect: { timeout: 10000 },
  outputDir: 'output/checkup-ui/results', reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:5180', ...offlineBrowser(),
    trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: { command: `"${process.execPath}" ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5180`, url: 'http://127.0.0.1:5180',
    reuseExistingServer: false,
    env: { VITE_ANALYSIS_MODE: 'checkup', VITE_API_BASE_URL: '', VITE_BAIDU_MAP_AK: 'offline-sdk-fixture',
      TEMP: resolve('output/checkup-ui'), TMP: resolve('output/checkup-ui') } },
});
