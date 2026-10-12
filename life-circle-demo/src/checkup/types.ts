import type { Center } from '../types';
import type { CheckupLayer, CheckupSnapshot, CheckupTaskView, FacilityExtensionView,
  FacilityRetryView, FacilityRoute, MajorCategory, RetainedCheckupView } from './contract';
import type { Contact, Reconnect } from './live';
import { STAGES, type LayerId, type Stage } from './validate';

/** v2 的引擎是具名的：两个算法各自服务各自的预算档，不互相顶替。 */
export type CheckupInput = {
  center: Center;
  engine: string;
  budget?: number;
  clientRequestId: string;
  /**
   * 这一次体检要评估的核心大类。不给就由后端用它自己的核心口径。
   *
   * 只放核心口径：扩展大类不进主请求，而是走按需补查 —— 一起发过来会让"31 个小类 × 4 块
   * = 124 次"撞上默认 60 次的预算，那是一个还没发请求就会被拒的任务。
   */
  categories?: MajorCategory[];
};

/**
 * 刷新后找回任务所需的全部信息。只存标识和输入，不存结果：结果、报告和图层一律
 * 回后端按任务 ID 与修订号重取，本地存的旧结果可能已经不是服务端认的那一版。
 */
export type CheckupHandle = {
  continuation?: { clientRequestId: string; baseRevision: number };
  input: CheckupInput;
  taskId?: string;
  cancelRequested?: boolean;
  savedAt: number;
};

/** 阶段是后端说的，界面只按它排序和显示进度；没有百分比，也没有"预计剩余"。 */
export type CheckupPhase =
  | 'idle' | 'submitting' | 'restoring' | 'queued' | 'running' | 'fetching' | 'cancelling'
  | 'completed' | 'cancelled' | 'error';

export type CheckupState = {
  phase: CheckupPhase;
  /** 这一轮任务提交时的条件：刷新后还没取到结果时，界面靠它说明"在算哪个点"。 */
  input?: CheckupInput;
  /** 与后端的连接中断、正在退避重连；任务本身没有因此失败。 */
  connection?: 'lost';
  /** 连接中断从何时起、已连续失败几次；连上即清掉。 */
  reconnect?: Reconnect;
  /** 最近一次拿到任务视图的时刻与其中的服务端时刻：界面据此推算"服务端此刻"。 */
  contact?: Contact;
  /**
   * 恢复时遇到的两种"找不回来"：`unconfirmed` 是创建请求没送达（按请求标识查不到），
   * `expired` 是任务 ID 服务端已不认识。两者的出路不同：前者可沿用同一请求标识重提，
   * 后者只能重新体检。
   */
  recovery?: 'unconfirmed' | 'expired';
  task?: CheckupTaskView;
  snapshot?: CheckupSnapshot;
  /** 已取到的图层，按 "图层:修订" 缓存：同一次体检里重开面板不该再发一次请求。 */
  layers?: Partial<Record<LayerId, CheckupLayer>>;
  route?: FacilityRoute;
  routeError?: string;
  /**
   * 按需补查：独立于主任务的一轮。它失败或取消都不影响 `task`、`snapshot` 与报告 ——
   * 一次成功的体检不会因为多查了几类设施而看起来白做了。
   */
  extensions?: FacilityExtensionView[];
  extensionRunning?: boolean;
  extensionError?: string;
  /**
   * §5 B2 决策 1 的重试：**同一次体检**的下一轮，不是并列的一份结果。所以它成功之后
   * `task` 与 `snapshot` 会一起换成新修订（`report.ts` 与图层都跟着换版），而 `retry`
   * 只留下"这一轮花了多少、停在哪"。
   */
  retry?: FacilityRetryView;
  /** 同一次体检已经发生过的重试，含正在进行的那一次：刷新后靠它把轮询接上。 */
  retries?: FacilityRetryView[];
  retrying?: boolean;
  retryError?: string;
  /**
   * §5 B2 决策 2：明细到期之后仍然读到的那一部分。它在场就意味着**明细都不在了** ——
   * 修订、图层与点击路线必须同时清空，否则屏幕上会留下"从上一版抄下来"的设施。
   */
  retained?: RetainedCheckupView;
  error?: string;
};

export const BUSY_PHASES: CheckupPhase[] = ['submitting', 'restoring', 'queued', 'running', 'fetching', 'cancelling'];

export function isCheckupBusy(state: CheckupState): boolean {
  return BUSY_PHASES.includes(state.phase);
}

/** 终态：轮询停下，界面不再等待。 */
export function isTerminal(task: CheckupTaskView | undefined): boolean {
  return task !== undefined
    && ['completed', 'failed', 'cancelled'].includes(task.status);
}

export const STAGE_LABELS: Record<Stage, string> = {
  isochrone: '等时圈',
  poi: '设施检索',
  accessibility: '服务覆盖',
  verification: '步行核验',
  reporting: '报告',
  ready: '完成',
};

/** 阶段推进只用于显示"进行到哪一步"：它不参与任何判断，也不是进度条。 */
export function isStageReached(stage: Stage, current: Stage | null | undefined): boolean {
  return current !== null && current !== undefined && STAGES.indexOf(stage) <= STAGES.indexOf(current);
}
