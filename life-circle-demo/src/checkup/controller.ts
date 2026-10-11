/**
 * v2 体检控制器：提交、轮询、取修订与图层、取消、恢复，以及"迟到的响应不许上屏"。
 *
 * 与旧分析控制器的分工一致，但有几处必须按 v2 的语义重写：
 *
 * - **修订是内容，不是序号。** 一份修订发布后永不改写，所以取回来的修订要与任务视图
 *   报告的修订号对得上；对不上就不渲染，而不是"用最新的覆盖一下"。
 * - **一个任务多条图层。** 图层按 `图层:修订` 缓存，同一版不重取；修订变了就整组丢掉，
 *   否则图上画的是上一版的灰区、面板里写的是这一版的面积。
 * - **任务不属于页面。** 控制器不随组件卸载而取消任务：卸载只是不再有人看，任务照跑。
 *   能让服务端停下的只有用户点的"取消"。任务句柄（请求标识、任务 ID、是否已请求取消）
 *   在发出创建请求**之前**就交给 `onHandle` 存起来，刷新后凭它向后端核对并接着看。
 * - **断网不是失败。** 轮询、取结果、取消遇到连接失败时退避重试并标出"连接中断"，
 *   网络回来就接上；只有服务端明确的回答（404、失败、修订不符）才会结束这一轮。
 *
 * 每个异步步骤结束回写状态之前都要问一次"这一轮还算数吗"（``current``）：取消、清除
 * 都会让旧的一轮作废，而它可能正好在这时返回。
 */
import type { CheckupLayer, CheckupTaskView, FacilityExtensionView, FacilityRetryView,
  MajorCategory, RetainedCheckupView } from './contract';
import type { LayerId } from './validate';
import { CheckupError, DETAIL_BUDGET_EXHAUSTED, DETAILS_EXPIRED, RETRY_IN_PROGRESS, isNotFound,
  type CheckupService } from './client';
import type { CheckupHandle, CheckupInput, CheckupState } from './types';
import { isCheckupBusy, isTerminal } from './types';
import { isRetryTerminal } from './retry';
import type { Contact } from './live';

type Run = {
  continuation?: { clientRequestId: string; baseRevision: number };
  input: CheckupInput;
  id?: string;
  abort: AbortController;
  revision: number;
  /** 服务端已经不认识这个任务 ID（404）：结果无法恢复，只能重新体检。 */
  expired?: boolean;
  /** 服务端明确拒绝创建（422、请求标识冲突……）：没有任务，重试要换新的请求标识。 */
  refused?: boolean;
  /** 按请求标识查过，服务端没有这个任务：创建请求没送达，等用户决定是否重提。 */
  unconfirmed?: boolean;
  /** 创建请求还在路上：取消只能先记下来，等拿到任务 ID 再发。 */
  creating?: boolean;
  cancelRequested?: boolean;
};

/** 连接失败时的退避：1、2、4、8 秒，之后每 10 秒一次，直到网络回来或用户离开这一轮。 */
export const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 10_000];
const POLL_MS = 1000;

/** 值得等一等的失败：连不上、超时、网关/上游暂时不可用。其余都是服务端的明确回答。 */
export function isTransient(error: unknown): boolean {
  return error instanceof CheckupError
    && ((error.status === 0 && error.code === 'network') || [502, 503, 504].includes(error.status));
}

/** 一次成功的回答：本机收到它的时刻，和它报的服务端时刻（旧版后端不报）。 */
function contactOf(task: CheckupTaskView): Contact {
  const serverTime = (task as Partial<CheckupTaskView>).serverTime;
  return { at: Date.now(), ...(typeof serverTime === 'number' && Number.isFinite(serverTime) ? { serverTime } : {}) };
}

const EXPIRED_MESSAGE = '服务端已找不到这个体检任务（数据目录被清理或更换了后端），结果无法恢复；可重新体检。';
const UNCONFIRMED_MESSAGE = '创建请求没有送达服务端：按请求标识查不到任务。为避免重复消耗额度，页面没有自动重新提交；'
  + '点"重新提交"会沿用同一请求标识，服务端若已建过任务会直接认出来，不会重复创建。';

export class CheckupController {
  state: CheckupState = { phase: 'idle' };
  private run?: Run;
  private revision = 0;
  private pendingStart = false;
  private wake?: () => void;

  constructor(
    private api: CheckupService,
    private publish: (state: CheckupState) => void,
    private onHandle: (handle: CheckupHandle | undefined) => void = () => {},
  ) {}

  private set(state: CheckupState) { this.state = state; this.publish(state); }
  private patch(state: Partial<CheckupState>) { this.set({ ...this.state, ...state }); }
  /** 又一次没连上：记下从何时起、连续失败了几次。 */
  private lost(failures: number) {
    this.patch({ connection: 'lost',
      reconnect: { since: this.state.connection === 'lost' && this.state.reconnect
        ? this.state.reconnect.since : Date.now(), attempts: failures } });
  }
  private current(run: Run) { return run.revision === this.revision && !run.abort.signal.aborted; }

  private save(run: Run) {
    this.onHandle({
      input: run.input, savedAt: Date.now(),
      ...(run.id ? { taskId: run.id } : {}),
      ...(run.cancelRequested ? { cancelRequested: true } : {}),
      ...(run.continuation ? { continuation: run.continuation } : {}),
    });
  }

  /**
   * 还有一个没结束的任务（或者一次没确认的创建）：这时不许再开新的。
   *
   * 旧版在这里是"先取消旧的再开新的"，那等于把换中心点当成了取消；现在只有用户点
   * "取消"才会停住服务端的任务，所以新任务只能等旧的结束。
   */
  get hasLiveTask(): boolean {
    const run = this.run;
    if (!run || run.refused || run.expired) return false;
    return !run.id || !isTerminal(this.state.task);
  }

  async start(input: Omit<CheckupInput, 'clientRequestId'>) {
    if (this.pendingStart || isCheckupBusy(this.state) || this.hasLiveTask) return;
    this.pendingStart = true;
    try {
      this.run?.abort.abort();
      const run: Run = {
        input: { center: input.center, engine: input.engine,
          ...(input.budget === undefined ? {} : { budget: input.budget }),
          clientRequestId: crypto.randomUUID() },
        abort: new AbortController(), revision: ++this.revision,
      };
      this.run = run;
      this.set({ phase: 'submitting', input: run.input });
      // 先存句柄再发请求：请求发出去之后页面才被关掉，刷新回来也能按请求标识找回任务。
      this.save(run);
      this.pendingStart = false;
      await this.execute(run);
    } finally { this.pendingStart = false; }
  }

  /**
   * 页面刷新或重新挂载后，凭存下来的句柄接着看。
   *
   * 有任务 ID 就直接问状态；没有（创建的响应丢了）就按请求标识查 —— 这是只读的，
   * 不会替用户重新提交。句柄里记着"已请求取消"的，再把取消发一遍（取消是幂等的）。
   */
  async resume(handle: CheckupHandle) {
    if (this.run) return;
    const run: Run = {
      input: handle.input, id: handle.taskId, cancelRequested: handle.cancelRequested,
      continuation: handle.continuation,
      abort: new AbortController(), revision: ++this.revision,
    };
    this.run = run;
    this.set({ phase: 'restoring', input: run.input });
    await this.follow(run);
  }

  async continueReport() {
    const old = this.run;
    const task = this.state.task;
    if (!old?.id || !task || !this.api.continueReport || this.pendingStart || isCheckupBusy(this.state)) return;
    if (!old.continuation && !task.completion?.canContinue) return;
    this.pendingStart = true;
    old.abort.abort();
    const run: Run = { ...old, abort: new AbortController(), revision: ++this.revision,
      cancelRequested: false, creating: true,
      continuation: old.continuation ?? { clientRequestId: crypto.randomUUID(), baseRevision: task.revision } };
    this.run = run;
    this.save(run);
    this.patch({ phase: 'submitting', error: undefined, recovery: undefined });
    try {
      const next = await this.api.continueReport(run.id!, run.continuation!);
      if (!this.current(run)) return;
      run.continuation = undefined;
      run.creating = false;
      this.save(run);
      this.patch({ task: next, phase: 'queued' });
    } catch (error) {
      run.creating = false;
      if (!this.current(run)) return;
      if (error instanceof CheckupError && error.status >= 400 && error.status < 500) {
        run.continuation = undefined;
        this.save(run);
        this.patch({ phase: 'error', error: error.message });
        return;
      }
      // An unclear POST is reconciled by its id, never repeated automatically.
    } finally { this.pendingStart = false; }
    await this.follow(run);
  }

  async retry() {
    if (this.pendingStart || isCheckupBusy(this.state)) return;
    const run = this.run;
    if (!run) { this.set({ phase: 'idle' }); return; }
    if (run.continuation) { await this.continueReport(); return; }
    const status = this.state.task?.status;
    if (run.expired || run.refused || status === 'failed' || status === 'cancelled') {
      // 过期、被拒、失败、已取消的任务不能再用同一个请求标识：后端会把它认成同一次请求。
      this.run = undefined;
      await this.start(run.input);
      return;
    }
    run.abort.abort();
    const next: Run = { ...run, abort: new AbortController(), revision: ++this.revision, unconfirmed: false };
    this.run = next;
    if (!next.id && run.unconfirmed) {
      // 用户点了"重新提交"：沿用同一请求标识，服务端若其实已经建过，会直接返回那一个。
      this.patch({ phase: 'submitting', error: undefined, connection: undefined, recovery: undefined });
      await this.execute(next);
      return;
    }
    this.patch({ phase: next.id ? 'running' : 'restoring', error: undefined, recovery: undefined });
    await this.follow(next);
  }

  /**
   * 换成"明细已到期"的那份视图，并**清掉**内存里所有含明细的东西。
   *
   * 清掉是这件事的实质，不是附带的卫生：修订里有设施名称、UID 与坐标，图层与点击路线
   * 也都是明细。留着它们，屏幕上就仍然在显示已经到了期的数据 —— 而那正是这条规则要
   * 阻止的事。迟到的响应由 `current()` 拦住：清掉之后这一轮就不再算数。
   */
  private async showRetained(run: Run, task: CheckupTaskView) {
    await this.switchToRetained(task, run);
  }

  private async switchToRetained(task: CheckupTaskView, run?: Run) {
    if (run !== undefined && !this.current(run)) return;
    if (this.state.task?.taskId !== task.taskId) return;
    let retained: RetainedCheckupView | undefined;
    try {
      retained = await this.api.retainedResult(task.taskId, task.revision);
    } catch {
      // 连汇总都读不到（旧修订没有留存汇总）：仍然要把明细清掉，并说清是哪条期限，
      // 而不是回退到"显示原来的报告" —— 那份报告已经不该被显示了。
      retained = undefined;
    }
    if (this.state.task?.taskId !== task.taskId) return;
    this.set({ ...this.state, phase: 'completed', task, snapshot: undefined, retained,
      layers: undefined, route: undefined, routeError: undefined, error: undefined });
  }

  private async execute(run: Run) {
    run.creating = true;
    let created: CheckupTaskView;
    try {
      // POST 不跟着任何页面动作一起中止：它回来的任务 ID 是唯一能取消服务端工作的东西。
      created = await this.api.create(toRequest(run.input));
    } catch (error) {
      run.creating = false;
      if (!this.current(run)) return;
      if (error instanceof CheckupError && error.status >= 400 && error.status < 500) {
        // 4xx 是服务端明确说了没建：句柄作废，重试会换新的请求标识。
        run.refused = true;
        this.onHandle(undefined);
        this.patch({ phase: 'error', error: error.message });
        return;
      }
      // 响应丢了（断网、超时、5xx、读不懂的响应）：服务端可能已经建了任务。只按请求标识
      // 去查，不自动重提。
      if (isTransient(error)) this.lost(1);
      await this.follow(run);
      return;
    }
    run.creating = false;
    run.id = created.taskId;
    this.save(run);
    if (!this.current(run)) return;
    this.patch({ task: created, contact: contactOf(created), phase: run.cancelRequested ? 'cancelling'
      : created.status === 'queued' ? 'queued' : 'running' });
    await this.follow(run);
  }

  /**
   * 一轮的主循环：先把任务 ID 找回来（如需要），把记着的取消发出去（如需要），再轮询到终态。
   * 连接失败按 `RECONNECT_DELAYS_MS` 退避重试；服务端的明确回答才会结束这一轮。
   */
  private async follow(run: Run) {
    let failures = 0;
    let cancelSent = false;
    while (this.current(run)) {
      try {
        if (run.continuation) {
          try {
            const confirmed = await this.api.byRequest(run.continuation.clientRequestId);
            if (!this.current(run)) return;
            if (confirmed.taskId !== run.id) throw new CheckupError('续查任务不匹配', 0, 'mismatched_task');
            run.continuation = undefined;
            this.save(run);
            this.patch({ task: confirmed });
          } catch (error) {
            if (!isNotFound(error)) throw error;
            const task = await this.api.status(run.id!, run.abort.signal);
            if (!this.current(run)) return;
            const revision = task.completion?.reportRevision;
            if (!this.state.snapshot && revision) {
              const snapshot = await this.api.result(run.id!, revision, run.abort.signal);
              if (!this.current(run)) return;
              this.patch({ snapshot });
            }
            this.patch({ phase: 'error', task, recovery: 'unconfirmed',
              error: '续查请求尚未送达，点击重新提交会沿用同一请求标识，避免重复扣费。' });
            return;
          }
        }
        if (!run.id) {
          let found: CheckupTaskView;
          try { found = await this.api.byRequest(run.input.clientRequestId); }
          catch (error) {
            if (!isNotFound(error)) throw error;
            if (!this.current(run)) return;
            if (run.cancelRequested) {
              // 服务端没有这个任务，取消的目的已经达到了。
              this.onHandle(undefined);
              this.run = undefined;
              this.set({ phase: 'cancelled', input: run.input });
              return;
            }
            run.unconfirmed = true;
            this.patch({ phase: 'error', error: UNCONFIRMED_MESSAGE, connection: undefined,
              recovery: 'unconfirmed' });
            return;
          }
          if (!this.current(run)) return;
          run.id = found.taskId;
          this.save(run);
          this.patch({ task: found, contact: contactOf(found), connection: undefined, reconnect: undefined });
        }
        const task = run.cancelRequested && !cancelSent && !isTerminal(this.state.task)
          ? await this.api.cancel(run.id) : await this.api.status(run.id, run.abort.signal);
        if (run.cancelRequested) cancelSent = true;
        failures = 0;
        if (!this.current(run)) return;
        this.patch({ connection: undefined, reconnect: undefined, contact: contactOf(task) });
        if (await this.accept(run, task)) return;
      } catch (error) {
        if (!this.current(run)) return;
        if (isTransient(error)) {
          this.lost(failures + 1);
          await this.pause(run, RECONNECT_DELAYS_MS[Math.min(failures++, RECONNECT_DELAYS_MS.length - 1)]);
          continue;
        }
        if (run.id && isNotFound(error)) {
          run.expired = true;
          this.patch({ phase: 'error', error: EXPIRED_MESSAGE, connection: undefined, recovery: 'expired' });
          return;
        }
        this.patch({ phase: 'error', connection: undefined,
          error: run.cancelRequested ? '未能确认取消，请再次取消或检查后端状态'
            : error instanceof Error ? error.message : '体检失败，请重试' });
        return;
      }
      await this.pause(run, POLL_MS);
    }
  }

  private async accept(run: Run, task: CheckupTaskView): Promise<boolean> {
    if (!this.current(run)) return true;
    // Restoring a running or interrupted round still shows the last frozen report.
    const previousRevision = task.completion?.reportRevision;
    if (!this.state.snapshot && previousRevision && task.status !== 'completed') {
      const previous = await this.api.result(run.id!, previousRevision, run.abort.signal);
      if (!this.current(run)) return true;
      this.patch({ snapshot: previous });
    }
    if (task.status === 'completed') {
      this.patch({ phase: 'fetching', task });
      // 任务视图每次都带着保留期：**先问它**。明细已经到期时一个明细字段都不去要 ——
      // 请求它只会换来一次必然的 410，而 410 不是错误，是"这份数据的寿命到了"。
      if (detailsGone(task)) { await this.showRetained(run, task); return true; }
      // 取的是任务视图报告的那一版修订，而不是"最新的一版"：两者不一致时，说明
      // 服务端还有一版没被这次轮询看到，取最新会让报告和任务状态描述不同的结论。
      let snapshot;
      try {
        snapshot = await this.api.result(run.id!, task.revision, run.abort.signal);
      } catch (error) {
        // 就在这两次请求之间到期了（或者别人刚清完）：换成保留视图，而不是报一句
        // "体检失败"——体检没有失败，是明细不再被授权查看。
        if (!isDetailsExpired(error)) throw error;
        await this.showRetained(run, task);
        return true;
      }
      if (!this.current(run)) return true;
      if (snapshot.revision !== task.revision) {
        throw new CheckupError('体检修订与任务状态不符，请检查服务版本', 0, 'mismatched_revision');
      }
      const layers: CheckupState['layers'] = {};
      // Switch all already-visible layers with the new report in one publication.
      const ids = Object.keys(this.state.layers ?? {}) as LayerId[];
      await Promise.all(ids.map(async id => {
        try { layers[id] = await this.api.layer(task.taskId, id, task.revision); }
        catch { /* The ordinary layer loader exposes a named failure after the switch. */ }
      }));
      if (!this.current(run)) return true;
      this.set({ phase: 'completed', input: run.input, task, snapshot, retained: undefined, layers });
      return true;
    }
    if (task.status === 'cancelled') { this.patch({ phase: 'cancelled', task }); return true; }
    if (task.status === 'failed') {
      this.patch({ phase: 'error', task,
        error: task.error ? `体检执行失败：${task.error}` : '体检执行失败，请检查后端配置后重试' });
      return true;
    }
    this.patch({ task, phase: task.status === 'queued' ? 'queued'
      : task.status === 'cancelling' || run.cancelRequested ? 'cancelling' : 'running' });
    return false;
  }

  private pause(run: Run, ms: number) {
    const signal = run.abort.signal;
    return new Promise<void>(resolve => {
      const finish = () => {
        clearTimeout(timer); signal.removeEventListener('abort', finish);
        if (this.wake === finish) this.wake = undefined;
        resolve();
      };
      const timer = setTimeout(finish, ms);
      signal.addEventListener('abort', finish, { once: true });
      this.wake = finish;
      if (signal.aborted) finish();
    });
  }

  /** 网络恢复（`online` 事件）或页面重新可见时调用：不等退避计时，马上再问一次。 */
  nudge() { this.wake?.(); }

  /**
   * 取一层图层。缓存键含修订号：同一版不重取，换版不沿用。
   *
   * 后端对"这一层还不存在"和"这一层这次没有内容"给的是两种回答（前者 409 带名字，
   * 后者是一份空集合），这里不把它们合并：把 409 显示成空图层，读者会以为那一层
   * 已经画过了。
   */
  async layer(layerId: LayerId): Promise<CheckupLayer | undefined> {
    const task = this.state.task;
    if (!task) throw new CheckupError('任务尚未创建，无法取图层', 0, 'no_task');
    // 明细已经到期：图层整组不可用。这里直接返回 undefined，让调用方知道"这一层没画"，
    // 而不是让它去撞一次必然的 410 再把它显示成一个图层错误。
    if (detailsGone(task) || this.state.retained !== undefined) return undefined;
    const revision = this.state.snapshot?.revision ?? task.revision;
    const cached = this.state.layers?.[layerId];
    if (cached && cached.revision === revision) return cached;
    let layer: CheckupLayer;
    try {
      layer = await this.api.layer(task.taskId, layerId, revision);
    } catch (error) {
      if (!isDetailsExpired(error)) throw error;
      await this.switchToRetained(task);
      return undefined;
    }
    // 取的过程里任务换了（清除后重开、或者修订又前进了一版）：这一张属于上一版，
    // 丢掉而不是画上去 —— 图上画着旧灰区、面板写着新面积是最难发现的一类错。
    const now = this.state.task;
    if (!now || now.taskId !== task.taskId || (this.state.snapshot?.revision ?? now.revision) !== revision) return undefined;
    this.patch({ layers: { ...this.state.layers, [layerId]: layer } });
    return layer;
  }

  /** 点击某个设施的详情路线。它不发布修订，所以结果只留在 `route` 里。 */
  async detail(facilityId: string) {
    const task = this.state.task;
    if (!task) return;
    this.patch({ route: undefined, routeError: undefined });
    try {
      const route = await this.api.route(task.taskId, facilityId);
      if (this.state.task?.taskId !== task.taskId) return;
      this.patch({ route });
    } catch (error) {
      if (this.state.task?.taskId !== task.taskId) return;
      if (isDetailsExpired(error)) {
        // 到期之后连"这一条设施"都不该再被点开：走的是同一条到期路径，不是一条路线错误。
        await this.switchToRetained(task);
        return;
      }
      const message = error instanceof CheckupError ? error.message : '未能取到这条设施的步行路线';
      this.patch({ routeError: message,
        ...(error instanceof CheckupError && error.code === DETAIL_BUDGET_EXHAUSTED ? { route: undefined } : {}) });
    }
  }

  /**
   * 用户明确的取消 —— 唯一会让服务端停下的动作。
   *
   * "已请求取消"先写进句柄再发请求：取消还没得到确认时刷新，回来会再发一遍（幂等）。
   * 创建请求还在路上时只能先记下，拿到任务 ID 立刻补发；创建没送达的，按请求标识查
   * 不到就说明服务端本来就没有这个任务，取消的目的已经达到。
   */
  async cancel() {
    const old = this.run;
    if (!old || old.cancelRequested && this.state.phase === 'cancelling') return;
    if (old.id && isTerminal(this.state.task) && !old.creating) return;
    if (old.refused || old.expired) { this.clear(); return; }
    old.cancelRequested = true;
    this.save(old);
    if (old.creating) { this.patch({ phase: 'cancelling', error: undefined }); return; }
    old.abort.abort();
    const run: Run = { ...old, abort: new AbortController(), revision: ++this.revision, unconfirmed: false };
    this.run = run;
    this.patch({ phase: 'cancelling', error: undefined, recovery: undefined });
    await this.follow(run);
  }

  /**
   * 忘掉当前这一个已经结束的任务（开始新体检前、或用户点"清除结果"）。
   *
   * 只对已结束、被拒、已过期或未送达的任务生效：还在跑的任务要先取消 —— 这里不替用户
   * 做这个决定。
   */
  clear() {
    const run = this.run;
    if (run && !run.refused && !run.expired && !run.unconfirmed && run.id && !isTerminal(this.state.task)) return false;
    if (run && !run.id && !run.unconfirmed && !run.refused) return false;
    ++this.revision;
    run?.abort.abort();
    this.run = undefined;
    this.onHandle(undefined);
    this.set({ phase: 'idle' });
    return true;
  }

  /** 不再发布状态、停止轮询；**不取消**服务端的任务。 */
  dispose() { this.publish = () => {}; this.run?.abort.abort(); ++this.revision; }

  // -- 按需补查 ----------------------------------------------------------

  /**
   * 对当前已完成的体检补查若干扩展大类。
   *
   * 补查是**独立一轮**：它有自己的标识和预算，不发布修订，也不改已经上屏的报告。所以
   * 这里的失败只写进 `extensionError`，绝不动 `phase`、`task` 和 `snapshot` —— 把补查的
   * 失败写成整个体检失败，会让一次成功的体检看起来白做了。
   */
  async extend(categories: MajorCategory[]) {
    const task = this.state.task;
    const revision = this.state.snapshot?.revision;
    if (task === undefined || revision === undefined || categories.length === 0) return;
    if (this.state.extensionRunning) return;
    this.patch({ extensionRunning: true, extensionError: undefined });
    const clientRequestId = crypto.randomUUID();
    try {
      const created = await this.api.extensionCreate(task.taskId, {
        schemaVersion: 'checkup-v1', clientRequestId, categories: [...categories],
      });
      if (this.state.task?.taskId !== task.taskId) return;
      this.patch({ extensions: mergeExtension(this.state.extensions, created) });
      const settled = await this.followExtension(task.taskId, created);
      if (this.state.task?.taskId !== task.taskId) return;
      this.patch({ extensions: mergeExtension(this.state.extensions, settled) });
    } catch (error) {
      if (this.state.task?.taskId !== task.taskId) return;
      // 后端在花钱之前就把"要多少次、剩多少次"写在 message 里：原样显示，不改写。
      this.patch({ extensionError: error instanceof CheckupError
        ? error.message : '补查没有完成，请稍后重试' });
    } finally {
      if (this.state.task?.taskId === task.taskId) this.patch({ extensionRunning: false });
    }
  }

  /** 补查的取消：只停这一次补查，不动主任务。 */
  async cancelExtension(extensionId: string) {
    const task = this.state.task;
    if (task === undefined) return;
    try {
      const view = await this.api.extensionCancel(task.taskId, extensionId);
      if (this.state.task?.taskId !== task.taskId) return;
      this.patch({ extensions: mergeExtension(this.state.extensions, view) });
    } catch (error) {
      if (this.state.task?.taskId !== task.taskId) return;
      this.patch({ extensionError: error instanceof CheckupError
        ? error.message : '未能取消这次补查' });
    }
  }

  /** 刷新后把已有的补查读回来：结果是服务端的，不靠本地记。 */
  async loadExtensions() {
    const task = this.state.task;
    if (task === undefined) return;
    try {
      const views = await this.api.extensionList(task.taskId);
      if (this.state.task?.taskId !== task.taskId) return;
      this.patch({ extensions: views });
    } catch {
      // 列表读不到不是体检的问题：不写 extensionError，面板显示"读不到"即可。
    }
  }

  /** 轮询到终态。终态仍要把视图写回列表里 —— 它带着最终的计数和原因。 */
  private async followExtension(taskId: string, created: FacilityExtensionView) {
    for (let attempt = 0; attempt < EXTENSION_POLL_LIMIT; attempt += 1) {
      if (isExtensionTerminal(created.status) || this.state.task?.taskId !== taskId) return created;
      await new Promise(resolve => setTimeout(resolve, EXTENSION_POLL_MS));
      if (this.state.task?.taskId !== taskId) return created;
      created = await this.api.extensionStatus(taskId, created.extensionId);
    }
    return created;
  }

  // -- §5 B2 决策 1 的重试 --------------------------------------------------

  /**
   * 继续把这次体检没查完的地段查下去。
   *
   * 与补查的三处不同，都是"它是同一次体检"的直接后果：
   *
   * * 它**改这一份报告**：一轮结束后任务与结果要重新按新修订读回来，图层整组丢掉
   *   （`layer()` 的缓存键含修订号，但已经取到的对象仍指向上一个版本）；
   * * 它**不改问题**：类别与圈面由后端从被冻结的那一版取，这里一个字段都不传；
   * * 它**一轮只跑一次**：后端对"这次体检已经有一次重试在进行中"给 409，界面据此把
   *   正在进行的那一轮接上，而不是把它显示成一次失败。
   *
   * 重复提交不会重复花钱：POST 的响应丢了之后再点一次，如果那一轮还在跑，服务端回
   * `checkup_retry_in_progress`；如果它已经跑完，这次体检要么已经查完（`not_needed`）、
   * 要么确实还需要一轮 —— 后者是用户真想要的。所以请求标识不必存进句柄。
   */
  async retryCheckup() {
    const task = this.state.task;
    const revision = this.state.snapshot?.revision;
    if (task === undefined || revision === undefined || this.state.retrying) return;
    this.patch({ retrying: true, retryError: undefined });
    try {
      let view: FacilityRetryView;
      try {
        view = await this.api.retryCreate(task.taskId, {
          schemaVersion: 'checkup-v1', clientRequestId: crypto.randomUUID(),
        });
      } catch (error) {
        if (!(error instanceof CheckupError) || error.code !== RETRY_IN_PROGRESS) throw error;
        // 已经有一轮在跑（很可能是刷新前发出去的那一次）：接上它，而不是报错。
        const running = (await this.api.retryList(task.taskId))
          .find(item => item.status === 'queued' || item.status === 'running');
        if (running === undefined) throw error;
        view = running;
      }
      if (this.state.task?.taskId !== task.taskId) return;
      this.patch({ retry: view, retries: mergeRetry(this.state.retries, view) });
      const settled = await this.followRetry(task.taskId, view);
      if (this.state.task?.taskId !== task.taskId) return;
      this.patch({ retry: settled, retries: mergeRetry(this.state.retries, settled) });
      if (isRetryTerminal(settled.status)) await this.refresh(task.taskId);
    } catch (error) {
      if (this.state.task?.taskId !== task.taskId) return;
      // 后端把"要多少次、剩多少次、为什么现在不行"写在 message 里：原样显示，不改写。
      this.patch({ retryError: error instanceof CheckupError
        ? error.message : '重试没有完成，请稍后重试' });
    } finally {
      if (this.state.task?.taskId === task.taskId) this.patch({ retrying: false });
    }
  }

  /** 停止这一轮重试。已发布的那一版报告照旧可读 —— 取消不会撤下已经取到的证据。 */
  async cancelRetry(retryId: string) {
    const task = this.state.task;
    if (task === undefined) return;
    try {
      const view = await this.api.retryCancel(task.taskId, retryId);
      if (this.state.task?.taskId !== task.taskId) return;
      this.patch({ retry: view, retries: mergeRetry(this.state.retries, view) });
    } catch (error) {
      if (this.state.task?.taskId !== task.taskId) return;
      this.patch({ retryError: error instanceof CheckupError ? error.message : '未能取消这次重试' });
    }
  }

  /**
   * 刷新后把已有的重试读回来，并把还在跑的那一轮接上。
   *
   * 结果一律以服务端为准：本地不存"重试跑到哪了"，因为一次刷新之后，本地那份要么是空的，
   * 要么是已经过期的。
   */
  async loadRetries() {
    const task = this.state.task;
    if (task === undefined) return;
    try {
      const views = await this.api.retryList(task.taskId);
      if (this.state.task?.taskId !== task.taskId) return;
      const latest = views[views.length - 1];
      const running = views.find(item => item.status === 'queued' || item.status === 'running');
      this.patch({ retries: views, ...(latest === undefined ? {} : { retry: latest }) });
      if (running !== undefined) {
        // 上一轮还在跑：继续跟着它，结束时照样把任务与结果按新修订读回来。
        const settled = await this.followRetry(task.taskId, running);
        if (this.state.task?.taskId !== task.taskId) return;
        this.patch({ retry: settled, retries: mergeRetry(this.state.retries, settled) });
        if (isRetryTerminal(settled.status)) await this.refresh(task.taskId);
      }
    } catch {
      // 列表读不到不是体检的问题：面板照旧显示已有的报告，只是看不到重试记录。
    }
  }

  /**
   * 一轮重试结束后把任务与结果重新读回来。
   *
   * 读的是**任务视图报告的那一版**，而不是"最新的一版"：重试正好在这两次请求之间又发布了
   * 一版时，取最新会让报告与任务状态描述不同的结论（与 `accept()` 同一条理由）。
   */
  private async refresh(taskId: string) {
    const task = await this.api.status(taskId);
    if (this.state.task?.taskId !== taskId) return;
    const snapshot = await this.api.result(taskId, task.revision);
    if (this.state.task?.taskId !== taskId) return;
    if (snapshot.revision !== task.revision) {
      throw new CheckupError('体检修订与任务状态不符，请检查服务版本', 0, 'mismatched_revision');
    }
    // 图层与详情路线一律丢掉：它们属于上一版修订，留着就是"图上画旧版、面板写新版"。
    this.set({ ...this.state, task, snapshot, layers: undefined,
      route: undefined, routeError: undefined });
  }

  /** 轮询到终态。终态的那一版带着这一轮最终的计数与停止原因。 */
  private async followRetry(taskId: string, created: FacilityRetryView) {
    let view = created;
    for (let attempt = 0; attempt < EXTENSION_POLL_LIMIT; attempt += 1) {
      if (isRetryTerminal(view.status) || this.state.task?.taskId !== taskId) return view;
      await new Promise(resolve => setTimeout(resolve, EXTENSION_POLL_MS));
      if (this.state.task?.taskId !== taskId) return view;
      view = await this.api.retryStatus(taskId, view.retryId);
    }
    return view;
  }
}

/** 同一次重试只保留最新的一条视图：轮询回来的那一版才是它的当前状态。 */
function mergeRetry(
  list: FacilityRetryView[] | undefined, view: FacilityRetryView,
): FacilityRetryView[] {
  const rest = (list ?? []).filter(item => item.retryId !== view.retryId);
  return [...rest, view];
}

/** 任务视图说这份明细已经到期了。判据只有服务端那一处。 */
function detailsGone(task: CheckupTaskView): boolean {
  return task.retention !== null && task.retention !== undefined
    && task.retention.detailsAvailable === false;
}

/** 明细接口的具名拒绝。它说的是"这份数据的寿命到了"，不是"这次请求失败"。 */
function isDetailsExpired(error: unknown): boolean {
  return error instanceof CheckupError && error.code === DETAILS_EXPIRED;
}

/** 同一个补查只保留最新的一条视图：轮询回来的那一版才是它的当前状态。 */
function mergeExtension(
  list: FacilityExtensionView[] | undefined, view: FacilityExtensionView,
): FacilityExtensionView[] {
  const rest = (list ?? []).filter(item => item.extensionId !== view.extensionId);
  return [...rest, view];
}

function isExtensionTerminal(status: FacilityExtensionView['status']): boolean {
  return status === 'completed' || status === 'partial' || status === 'failed'
    || status === 'cancelled';
}

/** 输入 → 请求体。`isochrone` 只在真的给了预算时才带上，让后端用它自己的默认档。 */
function toRequest(input: CheckupInput) {
  return {
    schemaVersion: 'checkup-v1' as const, clientRequestId: input.clientRequestId,
    engine: input.engine, center: { lng: input.center.lng, lat: input.center.lat },
    coordinateSystem: 'bd09ll' as const,
    ...(input.budget === undefined ? {} : { isochrone: { budget: input.budget } }),
    // 选中的核心口径才进主请求。扩展大类走按需补查：把它们一起发过来会让一次正常体检
    // 变成一次"31 个小类 × 4 块 = 124 次"的检索，而默认预算是 60 次 —— 那是必拒的。
    ...(input.categories === undefined ? {} : { facilities: { categories: [...input.categories] } }),
  };
}

/** 一次补查的轮询间隔与上限：补查只发一轮检索，没有成圈那种长阶段。 */
const EXTENSION_POLL_MS = 1000;
const EXTENSION_POLL_LIMIT = 600;
