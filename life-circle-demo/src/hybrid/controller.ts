/**
 * OSM＋百度旧版分析（`/api/v1/analysis/hybrid`）接到通用的旧版任务控制器上。
 *
 * 旧页面里任务挂在组件的 ref 上、卸载时按请求标识取消；现在任务交给模块里的控制器，
 * 页面只订阅。这里只回答 Hybrid 自己的三件事：错误归类、文案、结果是否属于这次提交。
 */
import type { Center } from '../types';
import type { HybridResultResponse, TaskStatusResponse } from '../api-contract';
import { LegacyController, type Failure, type LegacyApi, type LegacyHandle, type LegacyState } from '../legacyController';
import { createHybridClient, HybridApiError } from './client';

export type HybridInput = { center: Center; budget: number; clientRequestId: string };
export type HybridState = LegacyState<HybridInput, TaskStatusResponse, HybridResultResponse>;
type HybridClient = ReturnType<typeof createHybridClient>;

const MESSAGES: Record<string, string> = {
  baidu_walking_not_configured: '后端尚未配置百度步行服务',
  // 有意的拒绝，不是连不上：说成"请检查服务连接"会把部署配置问题指成网络问题。
  synthetic_mode_offline: '当前为离线合成模式，未接入百度端点核验，无法运行该分析',
  hybrid_busy: '服务正在处理其他分析，请稍后重试',
  hybrid_execution_failed: '分析执行失败，请检查数据配置',
  hybrid_invalid_response: '服务返回的结果格式异常',
  hybrid_task_mismatch: '结果与当前任务不匹配',
  hybrid_invalid_request: '分析参数无效，请检查坐标和预算',
  hybrid_request_id_conflict: '请求标识已用于不同的分析条件，请重新分析',
  hybrid_network: '无法连接分析服务或请求超时，请检查网络和服务地址',
};

export function classifyHybridError(error: unknown): Failure {
  if (!(error instanceof HybridApiError)) return 'other';
  if (error.status === 0 || [502, 504].includes(error.status)) return 'network';
  if (error.code === 'hybrid_busy') return 'busy';
  if (error.status === 404) return 'missing';
  if ((error.status >= 400 && error.status < 500) || error.status === 503) return 'refused';
  return 'other';
}

export function describeHybridError(error: unknown): string {
  if (error instanceof HybridApiError) {
    return `分析未完成：${error.detail ?? MESSAGES[error.code] ?? '请检查服务连接后重试'}`;
  }
  return error instanceof Error && error.message ? error.message : '分析未完成，请检查服务连接';
}

export function hybridApi(client: HybridClient = createHybridClient()): LegacyApi<HybridInput, TaskStatusResponse, HybridResultResponse> {
  return {
    create: input => client.create({ origin: input.center, coordinate_system: 'bd09ll',
      config: { max_baidu_requests: input.budget }, client_request_id: input.clientRequestId }),
    byRequest: key => client.byRequest(key),
    status: (id, signal) => client.status(id, signal),
    result: (id, signal) => client.result(id, signal),
    cancel: id => client.cancel(id),
    classify: classifyHybridError,
    describe: describeHybridError,
    matches: (result, task, input) => result.taskId === task.taskId
      && result.center.lng === input.center.lng && result.center.lat === input.center.lat
      && result.isochrone.config.max_baidu_requests === input.budget,
  };
}

export class HybridController extends LegacyController<HybridInput, TaskStatusResponse, HybridResultResponse> {
  constructor(
    client: HybridClient,
    publish: (state: HybridState) => void,
    onHandle?: (handle: LegacyHandle<HybridInput> | undefined) => void,
    recall?: () => LegacyHandle<HybridInput> | undefined,
  ) { super(hybridApi(client), publish, onHandle, recall); }
}
