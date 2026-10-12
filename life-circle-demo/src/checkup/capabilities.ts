/**
 * 能力表 → 工作台要显示的东西。
 *
 * 与 `report.ts` 同一条规矩：**只翻译，不判断**。哪些引擎可选、哪几档预算、余额怎么称呼，
 * 后端已经说清了，这里只把嵌套的字段读成平面的一行行文字。读不到就说读不到，绝不替后端
 * 编一个默认值 —— 界面自己凑一档预算发过去，正好撞上"不支持的预算一律 422"。
 *
 * 特别地：**不因为路网未配置就把引擎藏起来或禁用**。要不要拒绝由后端判（§4.1 不支持即
 * 422，且"是否支持"随部署而变，不是浏览器能算的）。界面能做的是把风险说在明处 ——
 * 让用户自己决定，而不是替他悄悄降级。
 */
import type { Capabilities, EngineOption } from './validate';
import { facilityCatalogView, type FacilityCatalogView } from './categories';
import { waterReviewRefs, type WaterReviewRef } from './water';

type RecordValue = Record<string, unknown>;
const object = (value: unknown): value is RecordValue =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0;
const number = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);
/** 步数上限是整数且至少 1；缺失或形状不对时不说 —— 0 步是一个关于后端的结论。 */
const stepCount = (value: unknown): value is number =>
  typeof value === 'number' && Number.isInteger(value) && value >= 1;

export type QuotaSummary = {
  /** 后端给的说法，逐字显示。缺了就说明缺了，不另起一个名字。 */
  label: string | null;
  lines: string[];
};

export type EngineView = EngineOption & {
  /** 选了它之前该知道的事：需要路网而路网没配好，或者引擎自己写的备注。 */
  caveat: string | null;
  /**
   * 必须摆在提交按钮旁边、不能收进说明里的那一部分：路网缺失会让提交被拒，合成替身画出的
   * 正圆和真实结果长得一样。普通备注是 null —— 它们留在说明里。
   */
  alert: string | null;
};

/** 后端给合成替身的标签后缀；有它就说明这一档画的是正圆，不是百度实测。 */
const SYNTHETIC_MARK = '合成替身';

export type CapabilityView = {
  engines: EngineView[];
  /** 首选引擎与首选预算：取后端给的默认值，不按档位大小猜。 */
  defaultEngine: string | null;
  defaultBudget: number | null;
  quota: QuotaSummary;
  poiRoundLimit: number | null;
  /** 当前部署采用的水系复核（用来认出早于复核的旧版本）；旧后端不给时为空。 */
  waterReviews: WaterReviewRef[];
  categoryDirectory: Array<{ id: string; label: string; order: number }>;
  /**
   * 检索计划与缓存的口径：本地处理上限、它与网络额度分不分家、缓存活在哪、首轮计划是不是
   * 额度预留。每条只在后端明确报了它时才出现；旧后端缺的项直接省略，不写 0 也不写
   * "(未配置)"——"没报"不是"没有"。
   */
  planningLines: string[];
  /** 当前 OSM 图状态；缺失表示旧版后端没有提供运行时状态。 */
  graphState: 'unloaded' | 'loading' | 'ready' | 'unavailable' | null;
  /** 当前部署采用的水系复核（用来认出早于复核的旧版本）；旧后端不给时为空。 */

  /**
   * 设施类别目录：类别选择器与预算算术的来源。旧后端不给这一项时为 null，界面据此
   * 说"这一版不提供类别选择"，而不是显示一份空的类别清单。
   */
  facilityCatalog: FacilityCatalogView | null;
};

function nested(source: RecordValue, ...path: string[]): unknown {
  let current: unknown = source;
  for (const key of path) {
    if (!object(current)) return undefined;
    current = current[key];
  }
  return current;
}

/** 余额只报后端报出来的那几项：这里多写一个数，就等于替账本说话。 */
export function quotaSummary(value: Capabilities): QuotaSummary {
  const quota = value.quota;
  const label = text(quota.label) ? quota.label : null;
  const lines: string[] = [];
  const tier = nested(quota, 'tier');
  const day = nested(quota, 'day');
  const daily = nested(quota, 'services', 'place', 'dailyBudget');
  if (text(tier)) lines.push(`本轮服务档位：${tier}`);
  if (text(day) && number(daily)) lines.push(`记账日：${day}`);
  const remaining = nested(quota, 'services', 'place', 'remainingToday');
  if (number(remaining) && number(daily)) {
    lines.push(`设施检索今日剩余：${remaining} / ${daily}`);
  } else if (daily === null && number(nested(quota, 'services', 'place', 'qps'))) {
    // 没有日额度只有速率：说明"今天花到多少"这件事不适用于这个服务，不能写成 0。
    const taskLimit = nested(value.budgets, 'poiRequests');
    lines.push(`设施检索不设本地每日额度；${number(taskLimit)
      ? `单次体检最多 ${taskLimit} 次，` : ''}仍受速率限制`);
  }
  return { label, lines };
}

/**
 * 检索计划与缓存的口径：把 `poiPlanning` 与 `cache` 读成工作台能显示的几行诊断。
 *
 * 两件事分得很清楚，因为它们的失败方式不同：**本地处理上限**（步数）与**网络额度**是两本
 * 账，缓存命中的页面只走前者；**首轮计划是执行前的估算，不是额度预留**，所以它不保证翻页
 * 与细分能在其中跑完。每条只在后端明确报了这一项时才出现。
 */
export function planningLines(value: Capabilities): string[] {
  const lines: string[] = [];
  const planning = object(value.poiPlanning) ? value.poiPlanning : null;
  const steps = planning === null ? undefined : planning.processingStepLimit;
  if (stepCount(steps)) lines.push(`本地处理步数上限：${steps} 步`);
  if (planning !== null && planning.networkBudgetIsSeparate === true) {
    lines.push('网络额度与本地处理上限分开计算');
  }
  if (planning !== null && planning.initialPlanIsReservation === false) {
    lines.push('首轮计划是执行前的估算，不是额度预留');
  }
  const cache = object(value.cache) ? value.cache : null;
  if (cache !== null && cache.processLocal === true) {
    lines.push('页面缓存为进程内存，重启即失效');
  }
  if (cache !== null && cache.crossTaskReuse === false) lines.push('未启用跨任务复用');
  return lines;
}

function engineView(engine: EngineOption, graphConfigured: boolean | null): EngineView {
  const notes = engine.notes.filter(text);
  const graphMissing = engine.requiresOsmGraph && graphConfigured === false;
  const caveat = graphMissing
    ? '该引擎需要本地 OSM 路网，本次部署未检测到路网缓存；提交后可能被后端拒绝。'
    : notes.length > 0 ? notes.join('；') : null;
  const alert = graphMissing || engine.label.includes(SYNTHETIC_MARK) ? caveat ?? engine.label : null;
  return { ...engine, notes, caveat, alert };
}

export function capabilityView(value: Capabilities): CapabilityView {
  const graph = nested(value.coverage, 'graphConfigured');
  const graphConfigured = typeof graph === 'boolean' ? graph : null;
  const graphStateValue = nested(value.coverage, 'graphState');
  const graphState = graphStateValue === 'unloaded' || graphStateValue === 'loading'
    || graphStateValue === 'ready' || graphStateValue === 'unavailable' ? graphStateValue : null;
  const engines = value.engines.map(engine => engineView(engine, graphConfigured));
  const first = engines[0] ?? null;
  return {
    engines,
    defaultEngine: first?.engineId ?? null,
    // 默认预算由后端指定，且必须在这一档里：校验层已经查过，这里不再兜底。
    defaultBudget: first?.defaultBudget ?? null,
    quota: quotaSummary(value),
    poiRoundLimit: number(value.budgets.poiRequests) && value.budgets.poiRequests > 0
      ? value.budgets.poiRequests : null,
    waterReviews: waterReviewRefs(value.waterReviews),
    categoryDirectory: value.categoryDirectory ?? [],
    planningLines: planningLines(value),
    graphState,

    facilityCatalog: facilityCatalogView(value),
  };
}

/** Hybrid-only historical guide; it is hidden until the local graph is ready. */
export function hybridTimeEstimate(budget: number | null, graphState: CapabilityView['graphState']): string | null {
  if (budget === null || graphState !== 'ready') return null;
  const scale = budget / 400;
  const minimum = Math.max(1, Math.round((191.2 * scale) / 60));
  const maximum = Math.max(minimum, Math.ceil((367.1 * scale) / 60));
  return `${minimum}–${maximum} 分钟`;
}

/** 换引擎就要换预算：档位是引擎自己的，沿用上一个引擎的档会撞 422。 */
export function budgetFor(view: CapabilityView, engineId: string): number | null {
  const engine = view.engines.find(item => item.engineId === engineId);
  return engine?.defaultBudget ?? null;
}

/** The next round's allowance comes from capabilities, not the frozen prior report. */
export function continuationLabel(restart: boolean, limit: number | null = null): string {
  return restart ? `复用边界，重新检索${limit === null ? '' : `（最多 ${limit} 次）`}`
    : `继续补全${limit === null ? '' : `，最多追加 ${limit} 次检索`}`;
}
