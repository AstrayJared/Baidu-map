import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({
  // Offline acceptance runs can isolate credentials without editing local env files.
  envDir: process.env.ANALYSIS_TEST_ENV_DIR || undefined,
  plugins: [react()],
  test: { include: ['src/**/*.test.ts'] },
  server: { port: 5173, strictPort: true, watch: { ignored: ['**/output/**', '**/test-results/**', '**/playwright-report/**'] } },
  build: { chunkSizeWarningLimit: 1300 }
});
