import { defineConfig, devices } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * 真实验收跑（§11.4）：真 AK、真 BMapGL 底图、真后端、真百度接口。
 *
 * 与 `playwright.checkup.config.ts` 的分工是**什么都不挡**：
 *
 * - 不 stub `/api/v2/*`，测的是运行中的后端（`VITE_API_BASE_URL`，默认 127.0.0.1:8000）；
 * - 不注入 `mapSdk`，页面自己按 `.env.local` 的真实浏览器 AK 加载 BMapGL；
 * - `VITE_BAIDU_MAP_AK` / `VITE_API_BASE_URL` **不在这里赋值**。Vite 的 `loadEnv` 里
 *   `process.env` 优先于 `.env.local`，所以一旦在这里写成空串或假 AK，跑的就是被挡住的那一套，
 *   而截图上会显示"地图不可用"——看起来还像通过了。
 *
 * 它会花真实额度，所以不进任何常规套件；一条用例跑一次完整体检，等到本轮修订发布为止。
 *
 * 端口用 5173 而不是另外开一个：后端的 `CORS_ORIGINS` 列的就是这个源。换个端口能起页面，
 * 但每一个 `/api/v2` 请求都会在浏览器里被 CORS 挡掉 —— 页面照样画地图、照样报错，
 * 看起来只是"接口没通"，而那时已经不知道该不该信这一轮的截图了。
 */
const out = resolve(process.env.CHECKUP_LIVE_OUTPUT_DIR ?? 'output/checkup-live');
mkdirSync(out, { recursive: true });
process.env.TEMP = out;
process.env.TMP = out;

export default defineConfig({
  testDir: './tests', testMatch: 'checkup-live.spec.ts', workers: 1, fullyParallel: false,
  // 真任务的上限是后端自己的 1800 秒（DEADLINE_SECONDS），这里留出取图层与截图的时间。
  timeout: 1700000, expect: { timeout: 20000 },
  outputDir: resolve(out, 'results'), reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:5173', ...devices['Desktop Edge'], channel: 'msedge',
    // 真实 SDK 请求 URL 含 AK，不将请求上下文写入 Trace 归档。
    viewport: { width: 1600, height: 1000 }, trace: 'off',
    screenshot: 'only-on-failure',
    // 单步操作必须有上限：默认是"一直等"，于是一个点不到的下拉会静默占满整个用例时限，
    // 而这一套的时限是 28 分钟 —— 看起来像"任务跑了很久"，实际上一笔额度都没花出去。
    actionTimeout: 30000, navigationTimeout: 60000 },
  webServer: { command: `"${process.execPath}" ./node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173`, url: 'http://127.0.0.1:5173',
    reuseExistingServer: false, timeout: 60000, env: { VITE_ANALYSIS_MODE: 'checkup' } },
});
