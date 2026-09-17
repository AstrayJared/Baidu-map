import { defineConfig, devices } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

const artifacts = process.env.INTEGRATION_OUTPUT_DIR || 'output/hybrid-integration';
const temp = resolve(artifacts, 'tmp');
mkdirSync(temp, { recursive: true });
process.env.TEMP = temp;
process.env.TMP = temp;
const python = process.env.ANALYSIS_TEST_PYTHON || resolve('../backend/.venv/Scripts/python.exe');
export default defineConfig({
  testDir: './tests/integration', fullyParallel: false, workers: 1,
  timeout: 30000, expect: { timeout: 10000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: resolve(artifacts, 'report') }]],
  outputDir: resolve(artifacts, 'results'),
  use: { baseURL: 'http://127.0.0.1:5178', ...devices['Desktop Edge'], channel: 'msedge',
    viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: [
    { command: `"${python}" -m uvicorn integration_app:app --app-dir tests --host 127.0.0.1 --port 8018 --no-access-log`,
      cwd: '../backend', url: 'http://127.0.0.1:8018/health', reuseExistingServer: false,
      env: { PYTHONPATH: resolve('../backend'), PYTHONPYCACHEPREFIX: resolve(temp, 'pycache') } },
    { command: 'npm run dev -- --port 5178', url: 'http://127.0.0.1:5178', reuseExistingServer: false,
      env: { VITE_ANALYSIS_MODE: 'api', VITE_API_BASE_URL: 'http://127.0.0.1:8018', VITE_BAIDU_MAP_AK: 'offline-sdk-fixture' } },
  ],
});
