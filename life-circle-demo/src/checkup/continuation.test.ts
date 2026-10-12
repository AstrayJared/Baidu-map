import { capabilityView, continuationLabel } from './capabilities';
import { describe, expect, it, vi } from 'vitest';
import { CheckupController } from './controller';
import { CheckupError, type CheckupService } from './client';
import { CENTER, capabilities, layer, route, snapshot, task } from './fixtures';
import type { CheckupCompletion, CheckupTaskView } from './contract';
import type { CheckupHandle, CheckupState } from './types';
import { validCompletion } from './validate';

const completion: CheckupCompletion = { reportRevision: 5, roundNumber: 1, roundPoiLimit: 60,
  roundPoiRequests: 60, cumulativePoiRequests: 60, routeRequests: 120, routeRemaining: 0,
  queryCompleteByMajor: { medical: false, shopping: true }, evaluatedCategories: 1, totalCategories: 2,
  evaluationStatus: 'partial', canContinue: true, restartRetrieval: false,
  stopReason: 'budget_exhausted', limitations: ['设施检索未完成'] };
const input = { center: CENTER, engine: 'baidu_e82', budget: 200 };
function service(): CheckupService {
  return { capabilities: vi.fn(async () => capabilities()), create: vi.fn(async () => task()),
    status: vi.fn(async () => task({ status: 'completed', stage: 'ready', revision: 5, completion })),
    result: vi.fn(async (_id, revision) => snapshot({ revision: revision ?? 5 })),
    layer: vi.fn(async (_id, layerId, revision) => layer({ layerId, revision })),
    cancel: vi.fn(async () => task({ status: 'cancelled' })), route: vi.fn(async () => route()),
    byRequest: vi.fn(async () => task()),
    continueReport: vi.fn(async () => task({ status: 'queued', completion })),
    extensionCreate: vi.fn(), extensionStatus: vi.fn(), extensionList: vi.fn(),
    extensionResult: vi.fn(), extensionCancel: vi.fn(), retryCreate: vi.fn(),
    retryStatus: vi.fn(), retryList: vi.fn(), retryCancel: vi.fn(), retainedResult: vi.fn(),
    sessionOpen: vi.fn(), sessionHeartbeat: vi.fn(), sessionClose: vi.fn() };
}

describe('manual report continuation', () => {
  it('submits once, keeps the report during work and switches visible layers atomically', async () => {
    const api = service();
    const states: CheckupState[] = [];
    const controller = new CheckupController(api, value => states.push(value));
    await controller.start(input);
    await controller.layer('facilities');
    let release!: (task: CheckupTaskView) => void;
    api.continueReport = vi.fn(() => new Promise<CheckupTaskView>(resolve => { release = resolve; }));
    const work = controller.continueReport();
    await controller.continueReport();
    expect(api.continueReport).toHaveBeenCalledTimes(1);
    expect(controller.state.snapshot?.revision).toBe(5);
    vi.mocked(api.status).mockResolvedValue(task({ status: 'completed', revision: 9,
      completion: { ...completion, roundNumber: 2 } }));
    release(task({ status: 'queued', revision: 5 }));
    await work;
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(controller.state.snapshot?.revision).toBe(9);
    expect(controller.state.layers?.facilities?.revision).toBe(9);
    expect(states.every(value => !value.layers?.facilities || value.layers.facilities.revision === value.snapshot?.revision)).toBe(true);
  });

  it('reconciles an unclear continuation POST without spending twice', async () => {
    const api = service();
    const handles: Array<CheckupHandle | undefined> = [];
    const controller = new CheckupController(api, () => {}, value => handles.push(value));
    await controller.start(input);
    api.continueReport = vi.fn(async () => { throw new CheckupError('offline', 0, 'network'); });
    vi.mocked(api.byRequest).mockResolvedValue(task({ status: 'completed', revision: 9 }));
    vi.mocked(api.status).mockResolvedValue(task({ status: 'completed', revision: 9 }));
    await controller.continueReport();
    expect(api.continueReport).toHaveBeenCalledTimes(1);
    expect(api.byRequest).toHaveBeenCalledTimes(1);
    expect(handles.some(value => value?.continuation?.baseRevision === 5)).toBe(true);
    expect(handles.at(-1)?.continuation).toBeUndefined();
    expect(controller.state.snapshot?.revision).toBe(9);
  });

  it('restores an interrupted round with its previous report and sends no POST', async () => {
    const api = service();
    vi.mocked(api.status).mockResolvedValue(task({ status: 'failed', revision: 7,
      error: 'interrupted_by_restart', completion }));
    const controller = new CheckupController(api, () => {});
    await controller.resume({ input: { ...input, clientRequestId: 'initial' }, taskId: 'task-1', savedAt: 0 });
    expect(controller.state.phase).toBe('error');
    expect(controller.state.snapshot?.revision).toBe(5);
    expect(api.create).not.toHaveBeenCalled();
    expect(api.continueReport).not.toHaveBeenCalled();
  });

  it('validates limits while accepting historical responses with no continuation fields', () => {
    expect(validCompletion(undefined)).toBe(true);
    expect(validCompletion(completion)).toBe(true);
    expect(validCompletion({ ...completion, roundPoiLimit: 1200, roundPoiRequests: 1200 })).toBe(true);
    expect(validCompletion({ ...completion, roundPoiLimit: 1200, roundPoiRequests: 1201 })).toBe(false);
    expect(validCompletion({ ...completion, roundPoiRequests: 61 })).toBe(false);
    expect(validCompletion({ ...completion, routeRequests: 121 })).toBe(false);
    expect(validCompletion({ ...completion, evaluatedCategories: 3 })).toBe(false);
  });

  it('keeps the prior report after refreshing an undelivered continuation', async () => {
    const api = service();
    vi.mocked(api.byRequest).mockRejectedValue(new CheckupError('missing', 404, 'checkup_task_not_found'));
    const controller = new CheckupController(api, () => {});
    await controller.resume({ input: { ...input, clientRequestId: 'initial' }, taskId: 'task-1', savedAt: 0,
      continuation: { clientRequestId: 'next', baseRevision: 5 } });
    expect(controller.state.recovery).toBe('unconfirmed');
    expect(controller.state.snapshot?.revision).toBe(5);
    expect(api.continueReport).not.toHaveBeenCalled();
  });
});

it('uses the next-round capability allowance rather than the historical report limit', () => {
  const value = capabilities();
  value.budgets.poiRequests = 1200;
  const view = capabilityView(value);
  expect(view.poiRoundLimit).toBe(1200);
  expect(continuationLabel(completion.restartRetrieval, view.poiRoundLimit)).toContain('1200');
  expect(continuationLabel(true, view.poiRoundLimit)).toBe('复用边界，重新检索（最多 1200 次）');
  expect(continuationLabel(false)).toBe('继续补全');
});
