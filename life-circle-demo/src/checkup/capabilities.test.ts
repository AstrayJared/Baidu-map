/**
 * 能力表测三件事：**读不出来的时候有没有说实话**、**不因为路网没配好就把引擎藏起来**，
 * 以及**检索计划与缓存的口径**。
 *
 * 第二条是这个文件里最要紧的一条：要不要拒绝某个引擎是后端的判断（§4.1 不支持即 422），
 * 浏览器替它决定一次，就等于把"这个部署支持什么"写死在了一份前端构建里。
 *
 * 第三条盯的是两本不能混的账：本地处理上限（步数，缓存重放也算）与网络额度（只有真的
 * 发出去的调用才算）。旧后端没报的字段一律省略，不写 0 也不写"(未配置)"。
 */
import { describe, expect, it } from 'vitest';
import { budgetFor, capabilityView, hybridTimeEstimate, planningLines, quotaSummary } from './capabilities';
import { validCapabilities } from './validate';
import { capabilities, engine } from './fixtures';

describe('capabilities document', () => {
  it('accepts the fixture', () => {
    expect(validCapabilities(capabilities())).toBe(true);
  });

  it('refuses an engine whose default budget is not one of its tiers', () => {
    // 默认预算不在档位里，说明后端和这份能力表至少有一处是错的；两边都别信。
    expect(validCapabilities(capabilities({
      engines: [engine({ defaultBudget: 300 })] }))).toBe(false);
    expect(validCapabilities(capabilities({ engines: [] }))).toBe(false);
    expect(validCapabilities(capabilities({ engines: [engine({ budgets: [0, 400] })] }))).toBe(false);
    expect(validCapabilities(capabilities({
      engines: [engine({ engineId: '' })] }))).toBe(false);
  });
});

describe('engine list', () => {
  it('keeps the engines in the order the backend lists them', () => {
    const view = capabilityView(capabilities());
    expect(view.engines.map(item => item.engineId)).toEqual(['baidu_e82', 'osm_hybrid']);
    expect(view.defaultEngine).toBe('baidu_e82');
    expect(view.defaultBudget).toBe(400);
  });

  it('never hides an engine that needs a road network it cannot see', () => {
    const view = capabilityView(capabilities({ coverage: { graphConfigured: false } }));
    // 两个引擎都还在：选不选得住是后端说了算。
    expect(view.engines).toHaveLength(2);
    expect(view.engines[0].caveat).toBeNull();
    expect(view.engines[1].caveat).toContain('未检测到路网缓存');
  });

  it('says nothing about the road network when the backend did not say', () => {
    // `graphConfigured` 缺失是"不知道"，不是"没有"：拿不知道当没有，会凭空警告一次。
    // 引擎自己的备注照旧带出来 —— 那是它对自己的说明，不依赖路网配置。
    const view = capabilityView(capabilities({ coverage: {},
      engines: [engine({ requiresOsmGraph: true })] }));
    expect(view.engines[0].caveat).toBeNull();
    const noted = capabilityView(capabilities({ coverage: {} }));
    expect(noted.engines[1].caveat).not.toContain('未检测到路网缓存');
  });

  it('carries the engine notes through so the choice is explained', () => {
    const view = capabilityView(capabilities({ coverage: {} }));
    expect(view.engines[1].notes).toEqual(['以本地 OSM 路网计算步行距离']);
    expect(view.engines[1].caveat).toBe('以本地 OSM 路网计算步行距离');
    expect(view.engines[0].notes).toEqual([]);
  });

  it('raises only a blocker or a stand-in next to the start button', () => {
    // 普通备注收进说明；路网缺失与合成替身必须在提交前就看得见。
    expect(capabilityView(capabilities({ coverage: {} })).engines.map(item => item.alert))
      .toEqual([null, null]);
    const missing = capabilityView(capabilities({ coverage: { graphConfigured: false } }));
    expect(missing.engines[1].alert).toContain('未检测到路网缓存');
    const synthetic = capabilityView(capabilities({ engines: [engine({
      label: '百度边界搜索（E8.2）· 合成替身', notes: ['边界是半径约 1080 米的正圆，不是百度实测'] })] }));
    expect(synthetic.engines[0].alert).toBe('边界是半径约 1080 米的正圆，不是百度实测');
  });

  it('switches the budget tier with the engine, because the tiers differ', () => {
    const view = capabilityView(capabilities());
    expect(budgetFor(view, 'osm_hybrid')).toBe(400);
    expect(budgetFor(view, 'unknown_engine')).toBeNull();
    expect(view.engines[1].budgets).toEqual([200, 400]);
  });
});

describe('quota line', () => {
  it('shows the backend wording verbatim, and invents none when it is missing', () => {
    const view = quotaSummary(capabilities());
    expect(view.label).toBe('本应用请求限制（不含浏览器 SDK、其他应用及旧接口流量）');
    // 余额的说法只能是后端给的：界面自己起个名字，读者就会以为它是账号总余额。
    expect(quotaSummary(capabilities({ quota: {} })).label).toBeNull();
  });

  it('reports the remaining place budget as the numbers it was given', () => {
    expect(quotaSummary(capabilities()).lines).toContain('设施检索今日剩余：1450 / 1600');
  });

  it('says "no daily budget" instead of reporting zero left', () => {
    // 日额度为 null 是"不适用"，写成 0 就是"今天已经用光了" —— 两种说法做的事完全不同。
    const summary = quotaSummary(capabilities({ quota: { label: '本应用请求限制', day: '2026-10-04',
      services: { place: { qps: 8, dailyBudget: null, remainingToday: null } } } }));
    expect(summary.lines).toContain('设施检索不设本地每日额度；单次体检最多 60 次，仍受速率限制');
    expect(summary.lines.some(line => line.includes('0 /'))).toBe(false);
    expect(summary.lines.some(line => line.startsWith('记账日'))).toBe(false);
  });
});

describe('planning and cache diagnostics', () => {
  it('reports the local processing ceiling as the step count the backend gave', () => {
    // 本地处理上限不是网络额度：缓存重放的页面也算步数，但不花额度。
    const view = capabilityView(capabilities());
    expect(view.planningLines).toContain('本地处理步数上限：4096 步');
    expect(view.planningLines).toContain('网络额度与本地处理上限分开计算');
    expect(view.planningLines).toContain('首轮计划是执行前的估算，不是额度预留');
  });

  it('says nothing about the planning fields an older backend does not report', () => {
    // 缺项不是 0，也不是"未配置"：那是替后端报了一个它没报的数。
    const lines = planningLines(capabilities({ poiPlanning: undefined }));
    expect(lines.some(line => line.includes('本地处理'))).toBe(false);
    expect(lines.some(line => line.includes('未配置'))).toBe(false);
    expect(lines.some(line => line.includes('0 步'))).toBe(false);
  });

  it('only says the two ceilings are separate when the backend says so', () => {
    const split = planningLines(capabilities({ poiPlanning: { processingStepLimit: 64 } }));
    expect(split).toContain('本地处理步数上限：64 步');
    expect(split).not.toContain('网络额度与本地处理上限分开计算');
    // 形状不对的步数（0、小数、字符串）不翻译成一句话，也不退化成 0 步。
    for (const bad of [0, 1.5, '4096']) {
      const lines = planningLines(capabilities({ poiPlanning: { processingStepLimit: bad } }));
      expect(lines.some(line => line.includes('本地处理'))).toBe(false);
    }
    // 报的是"不预留"才说估算：没报这一项时不替它下结论。
    expect(planningLines(capabilities({ poiPlanning: { initialPlanIsReservation: true } })))
      .not.toContain('首轮计划是执行前的估算，不是额度预留');
  });

  it('explains where the page cache lives and whether it is reused across tasks', () => {
    const view = capabilityView(capabilities());
    expect(view.planningLines).toContain('页面缓存为进程内存，重启即失效');
    expect(view.planningLines).toContain('未启用跨任务复用');
    // 旧后端没有 cache 字段：两种说法都不出现，而不是默认成"进程内存，重启即失效"。
    const older = planningLines(capabilities({ cache: undefined }));
    expect(older.some(line => line.includes('缓存'))).toBe(false);
    expect(older.some(line => line.includes('跨任务'))).toBe(false);
  });
});

describe('hybrid runtime guide', () => {
  it('only returns a budget guide after the OSM graph is ready', () => {
    expect(hybridTimeEstimate(400, 'loading')).toBeNull();
    expect(hybridTimeEstimate(400, null)).toBeNull();
    expect(hybridTimeEstimate(200, 'ready')).toBe('2–4 分钟');
    expect(hybridTimeEstimate(400, 'ready')).toBe('3–7 分钟');
  });
});

describe('facility catalog on the capability view', () => {
  it('translates the backend taxonomy once, for the selector to read', () => {
    // 类别选择器不自己解析能力表：它读的是这一层翻译出来的目录，所以这里必须转出来。
    const view = capabilityView(capabilities());
    expect(view.facilityCatalog).not.toBeNull();
    expect(view.facilityCatalog!.coreMajors).toEqual(['shopping', 'medical', 'education']);
    expect(view.facilityCatalog!.majors).toHaveLength(10);
    expect(view.facilityCatalog!.groups).toHaveLength(5);
    expect(view.facilityCatalog!.blocksUpperBound).toBe(4);
  });

  it('says the backend offers no selector instead of showing an empty one', () => {
    // 旧后端：给一个空目录会被读成"没有类别可选"，那是一个关于设施的结论。
    const view = capabilityView(capabilities({ facilityCategories: undefined }));
    expect(view.facilityCatalog).toBeNull();
  });
});
