/**
 * v2 体检的任务登记处：每个引擎一个控制器，活在模块里，不活在组件里。
 *
 * 组件挂载时订阅、卸载时退订 —— 退订不取消任务。切到另一个引擎、另一个页面再回来，
 * 看到的是同一个控制器、同一个任务；刷新页面则凭 `localStorage` 里的句柄向后端核对：
 * 有任务 ID 就问状态，没有就按请求标识查，结果、报告与图层一律重新向后端取。
 *
 * 本地只存两样东西：任务句柄（找回任务用）和界面偏好（中心草稿、预算档、图层开关、
 * 热力模式、报告是否打开）。不存结果本身 —— 结果以服务端按修订号给的为准。
 */
import { useSyncExternalStore } from 'react';
import { createCheckupService, type CheckupService } from './client';
import { startBrowserSession } from './browserSession';
import { CheckupController } from './controller';
import type { CheckupHandle, CheckupInput, CheckupState } from './types';
import { onWake } from '../wakeups';

export const CHECKUP_STORAGE_PREFIX = 'life-circle:checkup:v1:';

/** 界面偏好：只影响怎么看，不影响算什么。 */
export type CheckupPrefs = {
  draft?: { lng: number; lat: number };
  budget?: number;
  toggles?: Record<string, boolean>;
  serviceMode?: string;
  densityCategory?: string;
  reportOpen?: boolean;
  /** 左栏当前选项卡。 */
  tab?: string;
};

type Stored = { handle?: CheckupHandle; prefs?: CheckupPrefs };

export type CheckupSession = {
  engine: string;
  controller: CheckupController;
  getState: () => CheckupState;
  subscribe: (listener: () => void) => () => void;
  prefs: () => CheckupPrefs;
  setPrefs: (patch: Partial<CheckupPrefs>) => void;
};

const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0;

function validInput(value: unknown, engine: string): value is CheckupInput {
  const input = value as CheckupInput | null;
  return !!input && typeof input === 'object' && input.engine === engine && text(input.clientRequestId)
    && !!input.center && finite(input.center.lng) && finite(input.center.lat)
    && (input.budget === undefined || finite(input.budget));
}

/** 读回来的东西一律过一遍：旧版本或手改过的存档不能让页面起不来。 */
export function readStored(engine: string, storage: Storage | undefined = safeStorage()): Stored {
  if (!storage) return {};
  let raw: unknown;
  try { raw = JSON.parse(storage.getItem(CHECKUP_STORAGE_PREFIX + engine) ?? 'null'); }
  catch { return {}; }
  if (!raw || typeof raw !== 'object') return {};
  const { handle, prefs } = raw as Stored;
  const out: Stored = {};
  if (handle && validInput(handle.input, engine)
    && (handle.taskId === undefined || text(handle.taskId))) {
    out.handle = { input: handle.input, savedAt: finite(handle.savedAt) ? handle.savedAt : 0,
      ...(handle.taskId ? { taskId: handle.taskId } : {}),
      ...(handle.continuation && text(handle.continuation.clientRequestId)
        && Number.isInteger(handle.continuation.baseRevision) && handle.continuation.baseRevision > 0
        ? { continuation: handle.continuation } : {}),
      ...(handle.cancelRequested === true ? { cancelRequested: true } : {}) };
  }
  if (prefs && typeof prefs === 'object') {
    const p: CheckupPrefs = {};
    if (prefs.draft && finite(prefs.draft.lng) && finite(prefs.draft.lat)) p.draft = { lng: prefs.draft.lng, lat: prefs.draft.lat };
    if (finite(prefs.budget)) p.budget = prefs.budget;
    if (prefs.toggles && typeof prefs.toggles === 'object') {
      p.toggles = Object.fromEntries(Object.entries(prefs.toggles).filter(([, on]) => typeof on === 'boolean'));
    }
    if (text(prefs.serviceMode)) p.serviceMode = prefs.serviceMode;
    if (text(prefs.densityCategory)) p.densityCategory = prefs.densityCategory;
    if (typeof prefs.reportOpen === 'boolean') p.reportOpen = prefs.reportOpen;
    if (text(prefs.tab)) p.tab = prefs.tab;
    out.prefs = p;
  }
  return out;
}

function safeStorage(): Storage | undefined {
  try { return typeof localStorage === 'undefined' ? undefined : localStorage; }
  catch { return undefined; }
}

function write(engine: string, stored: Stored) {
  const storage = safeStorage();
  // 存不进去（隐私模式、配额满）时任务照跑，只是刷新后找不回来 —— 不为此打断用户。
  try { storage?.setItem(CHECKUP_STORAGE_PREFIX + engine, JSON.stringify(stored)); }
  catch { /* 见上 */ }
}

const sessions = new Map<string, CheckupSession>();
let sharedService: CheckupService | undefined;
let stopBrowserSession: (() => void) | undefined;

/**
 * 应用自己的那个服务对象，顺带把**浏览会话**打开并保活。
 *
 * 会话是整个应用一个，不是每个引擎一个：同一浏览器的两个标签页、两个体检引擎共用它
 * （§5 B2 决策 2）。注入服务（测试、或者别的宿主）时不碰会话 —— 那种情况下这个模块
 * 连浏览器都不该假设存在。
 */
function ensureService(service?: CheckupService): CheckupService {
  if (service !== undefined) return service;
  if (sharedService === undefined) {
    sharedService = createCheckupService();
    stopBrowserSession = startBrowserSession(sharedService);
  }
  return sharedService;
}

export function checkupSession(engine: string, service?: CheckupService): CheckupSession {
  const existing = sessions.get(engine);
  if (existing) return existing;
  const api = ensureService(service);
  const stored = readStored(engine);
  let state: CheckupState = { phase: 'idle' };
  let prefs: CheckupPrefs = stored.prefs ?? {};
  let handle = stored.handle;
  const listeners = new Set<() => void>();
  const controller = new CheckupController(api, next => {
    state = next;
    for (const listener of listeners) listener();
  }, next => { handle = next; write(engine, { handle, prefs }); });
  const session: CheckupSession = {
    engine, controller,
    getState: () => state,
    subscribe(listener) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    prefs: () => prefs,
    setPrefs(patch) { prefs = { ...prefs, ...patch }; write(engine, { handle, prefs }); },
  };
  sessions.set(engine, session);
  // 网络恢复、页面重新可见：在退避等待的控制器马上再问一次，不等计时器。
  onWake(() => controller.nudge());
  if (handle) void controller.resume(handle);
  return session;
}

/** 组件用：订阅某个引擎的体检状态。组件卸载只是退订，任务照跑。 */
export function useCheckupState(session: CheckupSession | null): CheckupState {
  return useSyncExternalStore(
    listener => session ? session.subscribe(listener) : () => {},
    () => session ? session.getState() : IDLE,
  );
}
const IDLE: CheckupState = { phase: 'idle' };

/** 测试用：丢掉所有控制器（不取消服务端任务），下一次取会话时重新从存储恢复。 */
export function resetCheckupSessions() {
  for (const session of sessions.values()) session.controller.dispose();
  sessions.clear();
  stopBrowserSession?.();
  stopBrowserSession = undefined;
  sharedService = undefined;
}
