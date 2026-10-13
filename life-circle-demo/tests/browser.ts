/**
 * 离线浏览器套件的启动环境，集中在一处。
 *
 * 下面几件事都和"这台机器上装了什么"有关，而和被测代码无关；散在每个配置里时，
 * 换一台机器就要改五六个文件，还会让"跑不起来"和"代码有问题"混成同一个红。
 *
 * - **用哪个浏览器。** 默认用 Playwright 自带的 Chromium：系统里没有 Edge 时，
 *   `channel: 'msedge'` 会让整套离线回归停在启动阶段，一个断言都没跑到。
 *   要指到系统浏览器就把 `PLAYWRIGHT_CHANNEL` 设成 `msedge`（真实底图验收那种）。
 * - **缺的共享库。** 自带 Chromium 在精简的 Linux 上缺 `libasound.so.2`，连
 *   `--version` 都跑不起来。`scripts/ensure-browser-libs.sh` 把发行版的运行库解到
 *   `.tmp/playwright-libs/`，这里只在该目录存在时把它接上 `LD_LIBRARY_PATH`：
 *   往系统里装东西不是测试该做的事。
 * - **产物目录。** 默认落在被忽略的 `output/` 下；以前默认写的是 `D:/…`，
 *   在别的平台上会在仓库里建出一个叫 `D:` 的目录。
 */
import { devices } from '@playwright/test';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';

/** 解包出来的运行库目录，由 `scripts/ensure-browser-libs.sh` 生成（仓库不收录）。 */
const BROWSER_LIBS = resolve('../.tmp/playwright-libs/root/usr/lib/x86_64-linux-gnu');

/**
 * 把解出的运行库接到本进程；Playwright 启动浏览器时子进程会继承环境变量。
 *
 * 离线套件在导入本模块时就自动接上；用**自带 Chromium 的其他配置**（真实底图验收就是）
 * 要显式调一次 —— 少了这一个库，浏览器连启动都启动不了，而报告里看起来像"用例失败"。
 */
export function useBrowserLibs(): string | null {
  if (process.platform !== 'linux' || !existsSync(BROWSER_LIBS)) return null;
  process.env.LD_LIBRARY_PATH = process.env.LD_LIBRARY_PATH
    ? `${BROWSER_LIBS}:${process.env.LD_LIBRARY_PATH}` : BROWSER_LIBS;
  return BROWSER_LIBS;
}

useBrowserLibs();

/** 离线套件的 `use` 片段：自带 Chromium，`PLAYWRIGHT_CHANNEL` 可指到系统浏览器。 */
export function offlineBrowser() {
  const channel = process.env.PLAYWRIGHT_CHANNEL;
  return { ...devices['Desktop Chrome'], ...(channel ? { channel } : {}) };
}

/** 演示套件的产物目录；`DEMO_OUTPUT_DIR` 覆盖。配置与用例必须读同一个默认值。 */
export function demoArtifacts(): string {
  return process.env.DEMO_OUTPUT_DIR || 'output/isochrone-demo-regression';
}

/**
 * 演示套件的端口；`DEMO_PORT` 覆盖。
 *
 * 用例里的"外连"断言是按这个端口分辨本站请求的，所以它必须和配置读同一个值：
 * 配置换端口而用例还盯着 5173 时，页面自己的请求会被记成外部请求，断言以一种
 * 看不懂的方式失败。默认端口常被本机正在跑的应用占着（那一套必须复用不得），
 * 所以这里留一个出口。
 */
export function demoPort(): number {
  return Number(process.env.DEMO_PORT ?? 5173);
}

/** 集成套件要起的后端解释器；`ANALYSIS_TEST_PYTHON` 覆盖，虚拟环境的路径按平台取。 */
export function analysisPython(): string {
  const fallback = process.platform === 'win32'
    ? '../backend/.venv/Scripts/python.exe' : '../backend/.venv/bin/python';
  return process.env.ANALYSIS_TEST_PYTHON || resolve(fallback);
}
