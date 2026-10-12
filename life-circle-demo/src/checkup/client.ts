/**
 * v2 体检客户端（`/api/v2/checkups`）。
 *
 * 与旧 `/api/analyses` 客户端分开：两个契约的任务视图、阶段和修订语义都不一样，把它
 * 们塞进一个函数里就得靠 `if (v2)` 分支来分辨字段，而分支写错的表现是静默读到
 * `undefined`。这里每个方法的返回值都先过 `validate.ts`，不合法就抛错，调用方拿到的
 * 一定是校验过的对象。
 *
 * 后端把错误写成 `{code, message}`：`code` 是机器可读的原因（`checkup_result_not_ready`、
 * `checkup_detail_budget_exhausted`……），`message` 是给读者的话。两个都留下 —— 界面用
 * `message` 显示，逻辑用 `code` 判断，不要拿 `message` 去比对字符串。
 */
import type { CheckupLayer, CheckupRequest, CheckupSnapshot, CheckupTaskView,
  FacilityExtensionDocument, FacilityExtensionRequest, FacilityExtensionView, FacilityRetryRequest,
  FacilityRetryView, FacilityRoute, RetainedCheckupView, SessionOpenRequest,
  SessionView } from './contract';
import { validCapabilities, validFacilityExtensionDocument, validFacilityExtensionView,
  validFacilityRetryView, validFacilityRoute, validLayer, validRetainedCheckupView,
  validSessionView, validSnapshot, validTaskView, type Capabilities, type LayerId } from './validate';
import { sessionHeader } from './browserSession';

export class CheckupError extends Error {
  constructor(message: string, public status: number, public code: string) { super(message); }
}

/** 后端拒绝的原因码；界面按它决定"重试"还是"改条件"。 */
export const NOT_READY = 'checkup_result_not_ready';
export const DETAIL_BUDGET_EXHAUSTED = 'checkup_detail_budget_exhausted';
export const ROUTE_UNAVAILABLE = 'checkup_route_unavailable';
/** 补查在花钱之前就被拒的两个原因：界面要把"要多少次、剩多少次"原样显示出来。 */
export const EXTENSION_BUDGET_TOO_SMALL = 'checkup_extension_budget_too_small';
export const EXTENSION_DAILY_BUDGET = 'checkup_extension_daily_budget';
/** 明细已按保留期到期：它**不是**可重试错误，界面要换成"还能看什么"。 */
export const DETAILS_EXPIRED = 'checkup_details_expired';
/** 重试被拒的四个原因；它们各自对应完全不同的下一步，所以不能合并成一句"失败"。 */
export const RETRY_NOT_NEEDED = 'checkup_retry_not_needed';
export const RETRY_IN_PROGRESS = 'checkup_retry_in_progress';
export const RETRY_DAILY_BUDGET = 'checkup_retry_daily_budget';
export const RETRY_BUDGET_TOO_SMALL = 'checkup_retry_budget_too_small';

export type CheckupService = {
  /** 能力表：能选哪个引擎、哪一档预算，以及本应用预算余额的说法，都从这里来。 */
  capabilities: (signal?: AbortSignal) => Promise<Capabilities>;
  create: (body: CheckupRequest) => Promise<CheckupTaskView>;
  continueReport?: (taskId: string, body: { clientRequestId: string; baseRevision: number }) => Promise<CheckupTaskView>;
  status: (taskId: string, signal?: AbortSignal) => Promise<CheckupTaskView>;
  byRequest: (clientRequestId: string) => Promise<CheckupTaskView>;
  result: (taskId: string, revision?: number, signal?: AbortSignal) => Promise<CheckupSnapshot>;
  layer: (taskId: string, layerId: LayerId, revision: number) => Promise<CheckupLayer>;
  cancel: (taskId: string) => Promise<CheckupTaskView>;
  route: (taskId: string, facilityId: string) => Promise<FacilityRoute>;
  /** 按需补查：复用原任务的圈面，只再查一次扩展类别。 */
  extensionCreate: (taskId: string, body: FacilityExtensionRequest) => Promise<FacilityExtensionView>;
  extensionStatus: (taskId: string, extensionId: string, signal?: AbortSignal) => Promise<FacilityExtensionView>;
  extensionList: (taskId: string, signal?: AbortSignal) => Promise<FacilityExtensionView[]>;
  extensionResult: (taskId: string, extensionId: string) => Promise<FacilityExtensionDocument>;
  extensionCancel: (taskId: string, extensionId: string) => Promise<FacilityExtensionView>;
  /**
   * §5 B2 决策 1 的重试：用新预算接着把这次体检没查完的地段查下去。
   *
   * 它与补查是**两个资源**，因为定稿之后做的事不同：补查产出并列的一份结果，重试为
   * **同一次体检**发布新修订 —— 所以这里没有"取回重试结果"的方法，新的答案在任务的
   * 下一个修订里（`result(taskId, revision)` 按指定版本读）。
   */
  retryCreate: (taskId: string, body: FacilityRetryRequest) => Promise<FacilityRetryView>;
  retryStatus: (taskId: string, retryId: string, signal?: AbortSignal) => Promise<FacilityRetryView>;
  retryList: (taskId: string, signal?: AbortSignal) => Promise<FacilityRetryView[]>;
  retryCancel: (taskId: string, retryId: string) => Promise<FacilityRetryView>;
  /**
   * §5 B2 决策 2 的浏览会话：开一个或回到已有的那一个、续租、说一声离开。
   *
   * 会话标识由 `browserSession.ts` 保管（会话在 localStorage、标签页在 sessionStorage），
   * 三个方法都通过请求头把它带给服务端 —— 它是"谁在问"，不是体检的参数。
   */
  sessionOpen: (body: SessionOpenRequest) => Promise<SessionView>;
  sessionHeartbeat: (sessionId: string, tabId: string) => Promise<SessionView>;
  sessionClose: (sessionId: string, tabId: string, keepalive?: boolean) => Promise<SessionView>;
  /**
   * 明细到期之后仍然可以读的那一部分（§5 B2 决策 2）。
   *
   * 它与 ``result`` 是两个问题：``result`` 问"这一版查到了什么"，它会因为到期而**具名
   * 拒绝**；这个方法问"到期之后还剩下什么"，它在明细被删掉之后依然成立。
   */
  retainedResult: (taskId: string, revision?: number) => Promise<RetainedCheckupView>;
};

const NOT_FOUND = new Set(['checkup_task_not_found', 'checkup_unknown_engine']);

/** 后端给了 `message` 就用它的，没给才用这些兜底文案。 */
const FALLBACK: Record<number, string> = {
  404: '任务不存在或已过期，请重新发起体检',
  409: '任务当前状态不允许该操作，请刷新任务状态后重试',
  422: '体检条件无效，请检查中心坐标、引擎与预算',
  429: '本任务的详情路线预算或服务配额已用尽，请稍后再试',
  503: '体检服务当前不可用，请联系管理员检查步行服务与路网数据配置',
};

export function createCheckupService(
  base = import.meta.env.VITE_API_BASE_URL?.trim() || '',
  fetcher: typeof fetch = fetch,
): CheckupService {
  const api = `${base.replace(/\/$/, '')}/api/v2`;
  const root = `${api}/checkups`;

  async function send(url: string, method: string, body?: unknown, signal?: AbortSignal,
                      keepalive = false) {
    const timeout = AbortSignal.timeout(15_000);
    let response: Response;
    try {
      response = await fetcher(url, {
        method, headers: { 'Content-Type': 'application/json', ...sessionHeader() },
        body: body === undefined ? undefined : JSON.stringify(body),
        // 页面卸载时的最后一次"我走了"要活得比页面久：普通请求会被卸载取消掉，
        // 于是最后一个标签页的离开就丢掉了，租约要等到宽限期后才自己到期。
        ...(keepalive ? { keepalive: true } : {}),
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      });
    } catch {
      // 调用方的取消不是故障：把它原样抛出去，重试逻辑才分得清"我取消的"和"断网了"。
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      throw new CheckupError('无法连接体检服务或请求超时，请检查网络和服务地址后重试', 0, 'network');
    }
    if (!response.ok) {
      const detail = await response.json().catch(() => null) as { code?: unknown; message?: unknown } | null;
      const code = typeof detail?.code === 'string' ? detail.code : `http_${response.status}`;
      const message = typeof detail?.message === 'string' && detail.message
        ? detail.message
        : FALLBACK[response.status] || '体检服务返回了未预期的错误，请稍后重试';
      throw new CheckupError(message, response.status, code);
    }
    return { response, body: await response.json().catch(() => undefined) as unknown };
  }

  const request = (path: string, method: string, body?: unknown, signal?: AbortSignal) =>
    send(`${root}${path}`, method, body, signal);

  /** 校验放在这里，不放调用方：漏掉一处校验就是一处静默的 `undefined`。 */
  function checked<T>(value: unknown, ok: (v: unknown) => boolean, label: string): T {
    if (!ok(value)) throw new CheckupError(`体检服务返回的${label}结构异常，请检查服务版本`, 0, 'invalid_response');
    return value as T;
  }

  return {
    async capabilities(signal) {
      // 能力表在 `/api/v2` 下，不在 `/checkups` 下：它是这一版接口的公共部分。
      const { body: value } = await send(`${api}/capabilities`, 'GET', undefined, signal);
      return checked<Capabilities>(value, validCapabilities, '能力表');
    },
    async create(body) {
      const { body: value } = await request('', 'POST', body);
      const task = checked<CheckupTaskView>(value, validTaskView, '任务视图');
      // 回来的必须是刚提交的那一次：错配的任务 ID 会让轮询去盯着别人的任务。
      if (task.clientRequestId !== body.clientRequestId) {
        throw new CheckupError('体检服务返回了另一次请求的任务，请检查服务版本', 0, 'mismatched_request');
      }
      return task;
    },
    async status(taskId, signal) {
      const { body: value } = await request(`/${encodeURIComponent(taskId)}`, 'GET', undefined, signal);
      const task = checked<CheckupTaskView>(value, validTaskView, '任务视图');
      if (task.taskId !== taskId) {
        throw new CheckupError('体检服务返回了另一个任务的状态，请检查服务版本', 0, 'mismatched_task');
      }
      return task;
    },
    async continueReport(taskId, body) {
      const { body: value } = await request(`/${encodeURIComponent(taskId)}/continue`, 'POST', body);
      const task = checked<CheckupTaskView>(value, validTaskView, '续查任务');
      if (task.taskId !== taskId) throw new CheckupError('续查任务不匹配', 0, 'mismatched_task');
      return task;
    },
    async byRequest(clientRequestId) {
      const { body: value } = await request(`/by-request/${encodeURIComponent(clientRequestId)}`, 'GET');
      const task = checked<CheckupTaskView>(value, validTaskView, '任务视图');
      if (task.clientRequestId !== clientRequestId) {
        throw new CheckupError('体检服务返回了另一次请求的任务，请检查服务版本', 0, 'mismatched_request');
      }
      return task;
    },
    async result(taskId, revision, signal) {
      const query = revision === undefined ? '' : `?revision=${revision}`;
      const { body: value } = await request(`/${encodeURIComponent(taskId)}/result${query}`, 'GET', undefined, signal);
      const snapshot = checked<CheckupSnapshot>(value, validSnapshot, '修订');
      // 要哪一版就检查哪一版：拿旧修订去渲染新结论是最难发现的一种错。
      if (snapshot.taskId !== taskId || (revision !== undefined && snapshot.revision !== revision)) {
        throw new CheckupError('体检修订与请求的任务或版本不符，请检查服务版本', 0, 'mismatched_revision');
      }
      return snapshot;
    },
    async layer(taskId, layerId, revision) {
      const path = `/${encodeURIComponent(taskId)}/layers/${encodeURIComponent(layerId)}?revision=${revision}`;
      const { body: value } = await request(path, 'GET');
      const layer = checked<CheckupLayer>(value, (v) => validLayer(v, layerId), '图层');
      if (layer.revision !== revision) {
        throw new CheckupError('图层与请求的修订不符，请检查服务版本', 0, 'mismatched_revision');
      }
      return layer;
    },
    async cancel(taskId) {
      const { body: value } = await request(`/${encodeURIComponent(taskId)}/cancel`, 'POST');
      return checked<CheckupTaskView>(value, validTaskView, '任务视图');
    },
    async route(taskId, facilityId) {
      const path = `/${encodeURIComponent(taskId)}/routes/${encodeURIComponent(facilityId)}`;
      const { body: value } = await request(path, 'POST');
      return checked<FacilityRoute>(value, validFacilityRoute, '设施路线');
    },
    async extensionCreate(taskId, body) {
      const path = `/${encodeURIComponent(taskId)}/facility-extensions`;
      const { body: value } = await request(path, 'POST', body);
      const view = checked<FacilityExtensionView>(value, validFacilityExtensionView, '补查状态');
      // 回来的必须是刚提交的那一次：错配的标识会让轮询盯着别人的补查。
      if (view.taskId !== taskId || view.clientRequestId !== body.clientRequestId) {
        throw new CheckupError('体检服务返回了另一次补查的状态，请检查服务版本', 0, 'mismatched_request');
      }
      return view;
    },
    async extensionStatus(taskId, extensionId, signal) {
      const path = `/${encodeURIComponent(taskId)}/facility-extensions/${encodeURIComponent(extensionId)}`;
      const { body: value } = await request(path, 'GET', undefined, signal);
      const view = checked<FacilityExtensionView>(value, validFacilityExtensionView, '补查状态');
      if (view.taskId !== taskId || view.extensionId !== extensionId) {
        throw new CheckupError('体检服务返回了另一次补查的状态，请检查服务版本', 0, 'mismatched_task');
      }
      return view;
    },
    async extensionList(taskId, signal) {
      const path = `/${encodeURIComponent(taskId)}/facility-extensions`;
      const { body: value } = await request(path, 'GET', undefined, signal);
      if (!Array.isArray(value)) {
        throw new CheckupError('体检服务返回的补查列表结构异常，请检查服务版本', 0, 'invalid_response');
      }
      const views = value.map(item =>
        checked<FacilityExtensionView>(item, validFacilityExtensionView, '补查状态'));
      // 列表里混进别的任务的补查，等于把一次越权访问当成一次成功读取。
      if (views.some(item => item.taskId !== taskId)) {
        throw new CheckupError('补查列表里出现了不属于本任务的记录，请检查服务版本', 0, 'mismatched_task');
      }
      return views;
    },
    async extensionResult(taskId, extensionId) {
      const path = `/${encodeURIComponent(taskId)}/facility-extensions/${encodeURIComponent(extensionId)}/result`;
      const { body: value } = await request(path, 'GET');
      const document = checked<FacilityExtensionDocument>(value, validFacilityExtensionDocument, '补查结果');
      if (document.taskId !== taskId || document.extensionId !== extensionId) {
        throw new CheckupError('补查结果与请求的任务或补查不符，请检查服务版本', 0, 'mismatched_revision');
      }
      return document;
    },
    async extensionCancel(taskId, extensionId) {
      const path = `/${encodeURIComponent(taskId)}/facility-extensions/${encodeURIComponent(extensionId)}/cancel`;
      const { body: value } = await request(path, 'POST');
      const view = checked<FacilityExtensionView>(value, validFacilityExtensionView, '补查状态');
      if (view.taskId !== taskId || view.extensionId !== extensionId) {
        throw new CheckupError('体检服务返回了另一次补查的状态，请检查服务版本', 0, 'mismatched_task');
      }
      return view;
    },
    async retryCreate(taskId, body) {
      const path = `/${encodeURIComponent(taskId)}/retries`;
      const { body: value } = await request(path, 'POST', body);
      const view = checked<FacilityRetryView>(value, validFacilityRetryView, '重试状态');
      // 回来的必须是刚提交的那一次：错配的标识会让轮询盯着别人的重试。
      if (view.taskId !== taskId || view.clientRequestId !== body.clientRequestId) {
        throw new CheckupError('体检服务返回了另一次重试的状态，请检查服务版本', 0, 'mismatched_request');
      }
      return view;
    },
    async retryStatus(taskId, retryId, signal) {
      const path = `/${encodeURIComponent(taskId)}/retries/${encodeURIComponent(retryId)}`;
      const { body: value } = await request(path, 'GET', undefined, signal);
      const view = checked<FacilityRetryView>(value, validFacilityRetryView, '重试状态');
      if (view.taskId !== taskId || view.retryId !== retryId) {
        throw new CheckupError('体检服务返回了另一次重试的状态，请检查服务版本', 0, 'mismatched_task');
      }
      return view;
    },
    async retryList(taskId, signal) {
      const path = `/${encodeURIComponent(taskId)}/retries`;
      const { body: value } = await request(path, 'GET', undefined, signal);
      if (!Array.isArray(value)) {
        throw new CheckupError('体检服务返回的重试列表结构异常，请检查服务版本', 0, 'invalid_response');
      }
      const views = value.map(item => checked<FacilityRetryView>(item, validFacilityRetryView, '重试状态'));
      // 列表里混进别的任务的重试，等于把一次越权访问当成一次成功读取。
      if (views.some(item => item.taskId !== taskId)) {
        throw new CheckupError('重试列表里出现了不属于本任务的记录，请检查服务版本', 0, 'mismatched_task');
      }
      return views;
    },
    async retryCancel(taskId, retryId) {
      const path = `/${encodeURIComponent(taskId)}/retries/${encodeURIComponent(retryId)}/cancel`;
      const { body: value } = await request(path, 'POST');
      const view = checked<FacilityRetryView>(value, validFacilityRetryView, '重试状态');
      if (view.taskId !== taskId || view.retryId !== retryId) {
        throw new CheckupError('体检服务返回了另一次重试的状态，请检查服务版本', 0, 'mismatched_task');
      }
      return view;
    },
    async retainedResult(taskId, revision) {
      const query = revision === undefined ? '' : `?revision=${revision}`;
      const path = `/${encodeURIComponent(taskId)}/retained-result${query}`;
      const { body: value } = await request(path, 'GET');
      const view = checked<RetainedCheckupView>(value, validRetainedCheckupView, '保留汇总');
      if (view.taskId !== taskId || (revision !== undefined && view.revision !== revision)) {
        throw new CheckupError('保留汇总与请求的任务或版本不符，请检查服务版本', 0,
          'mismatched_revision');
      }
      return view;
    },
    async sessionOpen(body) {
      const { body: value } = await request('/sessions', 'POST', body);
      const view = checked<SessionView>(value, validSessionView, '会话状态');
      // 回来的必须是这一个会话：错配的标识会让心跳去续租别人的会话。
      if (body.sessionId !== undefined && body.sessionId !== null && view.sessionId !== body.sessionId) {
        throw new CheckupError('体检服务返回了另一个会话的状态，请检查服务版本', 0, 'mismatched_task');
      }
      return view;
    },
    async sessionHeartbeat(sessionId, tabId) {
      const path = `/sessions/${encodeURIComponent(sessionId)}/tabs/${encodeURIComponent(tabId)}/heartbeat`;
      const { body: value } = await send(`${root}${path}`, 'POST');
      const view = checked<SessionView>(value, validSessionView, '会话状态');
      if (view.sessionId !== sessionId || view.tabId !== tabId) {
        throw new CheckupError('体检服务返回了另一个会话的状态，请检查服务版本', 0, 'mismatched_task');
      }
      return view;
    },
    async sessionClose(sessionId, tabId, keepalive = false) {
      const path = `/sessions/${encodeURIComponent(sessionId)}/tabs/${encodeURIComponent(tabId)}/close`;
      const { body: value } = await send(`${root}${path}`, 'POST', undefined, undefined, keepalive);
      return checked<SessionView>(value, validSessionView, '会话状态');
    },
  };
}

export function isNotFound(error: unknown): boolean {
  return error instanceof CheckupError && (error.status === 404 || NOT_FOUND.has(error.code));
}
