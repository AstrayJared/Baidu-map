/**
 * 测试用夹具：一份内部自洽的 v2 修订、任务视图与图层。
 *
 * 都是**合成**数据（中心在北京，设施是编的），只用来验证校验与流程；它们不代表任何
 * 真实社区，也不参与算法判据。每个字段都能被 `validate.ts` 接受，任何一处改动都要
 * 让它继续自洽 —— 夹具自相矛盾时，测试会去责怪被测代码。
 *
 * 所以这里刻意写得啰嗦：字段名、枚举、可空性全部照 `contract.ts` 写足，不用
 * `as unknown as` 蒙过去。夹具一旦靠断言绕过类型，它就再也证明不了契约本身。
 */
import type { CheckupLayer, CheckupSnapshot, CheckupTaskView, CoverageRow,
  FacilityExtensionDocument, FacilityExtensionView, FacilityRetryView, FacilityRoute,
  RetainedCheckupView, RetentionView, SessionView,
  ReportEvidence, ServiceZone, WaterDataEvidence } from './contract';
import type { Capabilities, EngineOption } from './validate';

export const CENTER = { lng: 116.404, lat: 39.915 };

/** 评估域面积；三类面积按 40% / 20% / 40% 拆，和下面的百分比一致。 */
export const AREA = 600_000;
export const CATEGORIES = ['shopping', 'medical', 'education'] as const;

export function polygon(offset = 0): Record<string, unknown> {
  const [x, y] = [116.39 + offset, 39.89];
  return { type: 'Polygon', coordinates: [[[x, y], [x + 0.02, y], [x + 0.02, y + 0.02],
    [x, y + 0.02], [x, y]]] };
}

/** 夹具任务的服务端时钟：创建后 2 秒开始，此刻是开始后 4.5 秒。 */
export const CREATED_AT = 1_700_000_000;
export const STARTED_AT = CREATED_AT + 2;
export const SERVER_TIME = STARTED_AT + 4.5;

export function task(overrides: Partial<CheckupTaskView> = {}): CheckupTaskView {
  return {
    taskId: 'task-1', clientRequestId: 'request-1', engine: 'baidu_e82', status: 'running',
    businessStatus: null, stage: 'accessibility', revision: 3, budget: 200, requests: 12,
    networkRequests: 12, elapsedSeconds: 4.5, createdAt: CREATED_AT, cancelRequested: false,
    error: null, serverTime: SERVER_TIME, startedAt: STARTED_AT, finishedAt: null,
    stageStartedAt: STARTED_AT + 3, lastActivityAt: SERVER_TIME - 0.5,
    // 默认"还在期限内、但还没记到期限"：它既不假装有到期时刻，也不假装明细已经没了。
    retention: retentionView(),
    progress: { step: 'category', label: '评估服务覆盖 · 医疗（第 2/3 类）', count: 120, limit: null,
      unit: '格', since: STARTED_AT + 4 },
    ...overrides,
  };
}

export function zone(overrides: Partial<ServiceZone> = {}): ServiceZone {
  return {
    id: 'zone-0', index: 0, categories: ['medical'], kind: 'single', areaM2: 12_500,
    parts: 1, cellIds: ['0:1:2'], labelVisible: true, suspected: true, evidenceGrade: 'model',
    queryStatus: 'complete', nearestFacility: 'synthetic:pharmacy-1',
    reason: 'beyond_service_distance', suggestion: '核查该片区的药店点位与通道接入。',
    geometry: polygon(), geometrySystem: 'metric', displayGeometry: polygon(), ...overrides,
  };
}

/** 报告里的覆盖行：九个分数栏 + 三类面积 + 证据等级。 */
export function row(overrides: Partial<CoverageRow> = {}): CoverageRow {
  return {
    category: 'shopping', supported: true, coverageLowerPct: 40, coverageUpperPct: 70,
    assessablePct: 80, unknownPct: 20, intervalWidthPct: 30, intervalDegenerate: false,
    unavailableReason: null, coveredM2: AREA * 0.4, gapM2: AREA * 0.2, unknownM2: AREA * 0.4,
    cells: {}, entrances: {}, evidenceGrade: 'model', ...overrides,
  };
}

const OVERALL = {
  available: true, reason: null, missingCategories: [], coverageLowerPct: 40,
  coverageUpperPct: 70, assessablePct: 80, unknownPct: 20, categories: [...CATEGORIES],
  weights: { shopping: 1 / 3, medical: 1 / 3, education: 1 / 3 },
};

export function report(overrides: Partial<ReportEvidence> = {}): ReportEvidence {
  return {
    reportId: 'task-1:5', generatedAt: 1_700_000_010, schemaVersion: 'checkup-v1',
    domain: polygon(), domainAreaM2: AREA, categories: CATEGORIES.map(category => row({ category })),
    overall: OVERALL,
    gaps: { status: 'partial', reason: null, zones: [zone()], byCategoryM2: { medical: 12_500 },
      compositeAreaM2: 0, gapAreaM2: 12_500, minLabelAreaM2: 2500, compositeMinCategories: 2,
      obstacleLayerAvailable: false, notes: [] },
    verification: { available: true, status: 'partial', provider: 'synthetic:checkup-routes',
      checked: 4, failed: 0, unresolved: 0, facilities: [], conflicts: [], spotChecks: [],
      spotCheckSummary: {}, localOverrides: [], queries: {}, reason: null, notes: [] },
    evidence: { schemaVersion: 'checkup-v1', ruleVersion: 'walk-distance-1000-v1',
      sourceResultHash: 'hash-4', views: {}, grid: {}, excludedAreaM2: 0,
      obstacleLayerAvailable: false, heatmapEstimated: true, catalogCompleteness: 'unverified',
      queryStatus: 'complete', notes: [] },
    limitations: ['目录完整性未经独立核实。'],
    dataSources: null,
    ...overrides,
  };
}

export function snapshot(overrides: Partial<CheckupSnapshot> = {}): CheckupSnapshot {
  return {
    schemaVersion: 'checkup-v1', taskId: 'task-1', revision: 5, generatedAt: 1_700_000_010,
    center: CENTER, coordinateSystem: 'bd09ll', stage: 'reporting', businessStatus: 'partial',
    engine: { engineId: 'baidu_e82', engineVersion: '1.5.0', label: '百度 E8.2' },
    isochrone: { geometry: polygon() },
    rules: { metric: 'walking_route', threshold_m: 1000, inclusive: true, tolerance_m: 100,
      assessment_scope: 'community', category_policy: 'per_category' },
    scope: { projection: 'local-multicross-e82', dataVersion: '2026-09-01',
      coverageSupported: true, assessmentDomainAvailable: true, excludedAreaM2: 0,
      modelSupportAvailable: true, notes: [] },
    trace: { dataVersions: {}, ruleVersions: {}, isochroneHash: 'abc', resultHash: 'hash-5',
      budgets: {}, recomputed: null, retried: null },
    facilities: null, facilitiesStatus: 'complete',
    accessibility: {
      status: 'partial', domain: polygon(), domainAreaM2: AREA, excludedAreaM2: 0, gridStepM: 50,
      refinedStepM: 25, maxLeafCells: 5000, searchCutoffM: 1100, views: {}, notes: [],
      categories: CATEGORIES.map(category => ({
        category, supported: true, coveredM2: AREA * 0.4, gapM2: AREA * 0.2, unknownM2: AREA * 0.4,
        domainAreaM2: AREA, unavailableReason: null, cells: {}, entrances: {}, coverage: null })),
    },
    accessibilityStatus: 'partial',
    serviceGaps: {
      status: 'partial', zones: [zone()], byCategoryM2: { medical: 12_500 }, compositeAreaM2: 0,
      gapAreaM2: 12_500, compositeMinCategories: 2, minLabelAreaM2: 2500,
      obstacleLayerAvailable: false, notes: [],
    },
    heatmap: { metric: 'walking_route', estimated: true, stepM: 50, domain: polygon(),
      categories: { medical: [{ cell: '0:0:0', lng: 116.4, lat: 39.9, distanceM: 320,
        nearestFacility: 'synthetic:pharmacy-1', status: 'covered' }] }, notes: [] },
    scores: {
      ruleVersion: 'walk-distance-1000-v1', domainAreaM2: AREA, formula: {},
      // 分数栏是报告行的子集：三类面积与证据等级只存在于报告里。
      categories: CATEGORIES.map(category => {
        const { coveredM2, gapM2, unknownM2, cells, entrances, evidenceGrade, ...score } =
          row({ category });
        return score;
      }),
      overall: OVERALL,
    },
    verification: {
      status: 'partial', provider: 'synthetic:checkup-routes', checked: 4, failed: 0,
      unresolved: 0, facilities: [], conflicts: [], spotChecks: [], spotCheckSummary: {},
      localOverrides: [],
      queries: { routeAttempts: 4, candidates: 6, checked: 4, unverified: 2, stopReason: null },
      reason: null, notes: [],
    },
    report: report(),
    water: null,
    warnings: [],
    ...overrides,
  };
}

/** 要素集合：设施、覆盖、灰区、热力、核验这五层实际返回的就是这个形态。 */
export function collection(features: Array<Record<string, unknown>> = [],
  properties: Record<string, unknown> = {}): Record<string, unknown> {
  return { type: 'FeatureCollection', coordinateSystem: 'bd09ll', features, properties };
}

export function feature(geometry: Record<string, unknown>,
  properties: Record<string, unknown> = {}): Record<string, unknown> {
  return { type: 'Feature', geometry, properties };
}

export function point(lng: number, lat: number): Record<string, unknown> {
  return { type: 'Point', coordinates: [lng, lat] };
}

export function layer(overrides: Partial<CheckupLayer> = {}): CheckupLayer {
  return {
    layerId: 'service_gaps', revision: 5, geometry: polygon(), displayGeometry: polygon(),
    document: null, resultHash: 'hash-5', ...overrides,
  };
}

export function engine(overrides: Partial<EngineOption> = {}): EngineOption {
  return {
    engineId: 'baidu_e82', label: '百度边界搜索（E8.2）', engineVersion: '1.5.0',
    budgets: [200, 400, 800], defaultBudget: 400, requiresOsmGraph: false, notes: [], ...overrides,
  };
}

/** 能力表：两个引擎各自带自己的预算档，余额的说法由后端给（§9 的"本应用预算余额"）。 */
export function capabilities(overrides: Partial<Capabilities> = {}): Capabilities {
  return {
    schemaVersion: 'checkup-v1',
    engines: [engine(), engine({ engineId: 'osm_hybrid', label: 'OSM＋百度',
      engineVersion: '1.5.0', budgets: [200, 400], requiresOsmGraph: true,
      notes: ['以本地 OSM 路网计算步行距离'] })],
    quota: { label: '本应用请求限制（不含浏览器 SDK、其他应用及旧接口流量）',
      tier: 'first-release', day: '2026-09-27',
      services: { direction: { qps: 16, maxInflight: 16, dailyBudget: null, spentToday: null,
        remainingToday: null },
      place: { qps: 8, maxInflight: 8, dailyBudget: 1600, spentToday: 150, remainingToday: 1450 } },
      matrixEnabled: false, claimsAccountBalance: false },
    budgets: { poiRequests: 60, routeRequests: 120, detailRouteRequests: 20,
      poiMinorCategories: { default: 14, all: 31 }, poiBlocksUpperBound: 4,
      // "分块上界 × 小类数"是冷启动首轮的**估计**：实际分块数由圈面决定（更少），
      // 翻页与细分还要更多页。旧字段保留但报 false —— 它曾把这个乘积说成下界。
      poiRequestsIsLowerBound: false, poiFirstRoundIsAnEstimate: true },
    // 本地处理上限与网络额度是两本账；首轮计划只是执行前的估算，不是额度预留。
    poiPlanning: { processingStepLimit: 4096, networkBudgetIsSeparate: true,
      initialPlanReportsCacheReuse: true, initialPlanIsReservation: false },
    coverage: { metricCrs: 'EPSG:3857', queryPaddingM: 50, graphConfigured: true,
      coverageBoundaryConfigured: true, completeDirectory: false },
    rules: { ruleVersion: 'walk-distance-1000-v1', statusThresholdSeconds: 900,
      assessmentScope: 'isochrone' },
    waterReviews: [],
    cache: { freshnessSeconds: null, crossTaskReuse: false, processLocal: true },
    facilityCategories: facilityCatalogFixture(),
    ...overrides,
  };
}

/**
 * 设施目录的 v2 视图：与后端 `catalog.facility_categories()` 同一个形状。
 *
 * 数字照当前目录写足（十类、31 个小类、核心三类 14 个），因为类别选择器的预算算术就是
 * 拿这些数去比的：夹具里写一个整好的数，测试就再也发现不了"界面的算术和后端的分叉"。
 */
export function facilityCatalogFixture(): Record<string, unknown> {
  return {
    version: 'poi-categories-v2.1',
    coreMajors: ['shopping', 'medical', 'education'],
    displayGroups: [
      { key: 'healthcare', label: '健康照护', order: 1, majors: ['medical', 'care'] },
      { key: 'education', label: '教育成长', order: 2, majors: ['education'] },
      { key: 'daily_life', label: '生活消费', order: 3, majors: ['shopping', 'dining', 'finance', 'life'] },
      { key: 'public_mobility', label: '公共出行', order: 4, majors: ['public', 'transport'] },
      { key: 'leisure', label: '文体休闲', order: 5, majors: ['leisure'] },
    ],
    majors: [
      { key: 'medical', label: '医疗健康', displayGroup: 'healthcare', core: true, minorCategories: 5 },
      { key: 'shopping', label: '购物消费', displayGroup: 'daily_life', core: true, minorCategories: 3 },
      { key: 'education', label: '教育', displayGroup: 'education', core: true, minorCategories: 6 },
      { key: 'care', label: '疗养康养', displayGroup: 'healthcare', core: false, minorCategories: 2 },
      { key: 'dining', label: '餐饮', displayGroup: 'daily_life', core: false, minorCategories: 2 },
      { key: 'finance', label: '金融', displayGroup: 'daily_life', core: false, minorCategories: 2 },
      { key: 'public', label: '政务公共服务', displayGroup: 'public_mobility', core: false, minorCategories: 3 },
      { key: 'leisure', label: '文体休闲', displayGroup: 'leisure', core: false, minorCategories: 3 },
      { key: 'transport', label: '交通出行', displayGroup: 'public_mobility', core: false, minorCategories: 3 },
      { key: 'life', label: '生活服务', displayGroup: 'daily_life', core: false, minorCategories: 2 },
    ],
  };
}

/**
 * 一次按需补查的状态：默认是"查完了、五个小类里取到三家"。
 *
 * 首轮估算与用量对齐：20 页处理里 16 页命中缓存，只有 4 次新增网络调用 —— 把这两本账
 * 混成一个数，就看不出缓存到底省下了什么。`stopReason` 默认 null（正常结束），需要诊断
 * 文案的用例自己覆盖它。
 */
export function extensionView(overrides: Partial<FacilityExtensionView> = {}): FacilityExtensionView {
  return {
    extensionId: 'extension-1', taskId: 'task-1', baseRevision: 5, clientRequestId: 'ext-request-1',
    status: 'completed', stage: 'ready', categories: ['dining', 'leisure'],
    budget: { limit: 60, spent: 20, remaining: 40 }, requests: 20, networkRequests: 4,
    facilitiesStatus: 'complete', countsByCategory: { dining: 1, leisure: 2 },
    stopReason: null,
    initialPlan: { initialPageCount: 20, reusableInitialPageCount: 16, estimatedNewInitialCalls: 4,
      remainingTaskBudget: 60, remainingDailyBudget: 1450, blocks: 4, minorCategories: 5,
      primaryQueriesOnly: true, isReservation: false },
    error: null, createdAt: CREATED_AT, finishedAt: CREATED_AT + 3, ...overrides,
  };
}

/**
 * 一次重试的状态：默认是"这一轮把缺口查完了，任务由此发布了新修订"。
 *
 * 它**不报**发布了第几版 —— 那一版是任务自己的最新修订，客户端照常读任务即可。两本账
 * （页面处理 / 新增网络调用）分开报，与补查同一个理由：把两者并成一个数，会让"额度用尽"
 * 看起来像"查了很多"。
 */
export function retryView(overrides: Partial<FacilityRetryView> = {}): FacilityRetryView {
  return {
    retryId: 'retry-1', taskId: 'task-1', baseRevision: 5, clientRequestId: 'retry-request-1',
    status: 'completed', stage: 'ready', budget: { limit: 240, spent: 63, remaining: 177 },
    requests: 63, networkRequests: 41, facilitiesStatus: 'completed', stopReason: null,
    initialPlan: { initialPageCount: 20, reusableInitialPageCount: 16,
      estimatedNewInitialCalls: 4, remainingTaskBudget: 240, remainingDailyBudget: 1900,
      blocks: 4, minorCategories: 3, primaryQueriesOnly: true, isReservation: false },
    error: null, createdAt: CREATED_AT, finishedAt: CREATED_AT + 5, ...overrides,
  };
}

/** 一次补查的结果文档：``group`` 存在才叫"查到了"，为 null 时必须带具名原因。 */
export function extensionDocument(
  overrides: Partial<FacilityExtensionDocument> = {},
): FacilityExtensionDocument {
  return {
    extensionId: 'extension-1', taskId: 'task-1', baseRevision: 5,
    categories: ['dining', 'leisure'], status: 'completed', facilitiesStatus: 'complete',
    group: { queryStatus: 'completed', catalogCompleteness: 'unverified',
      provider: 'synthetic:checkup-tests', apiVersion: '3.0', dataSource: 'synthetic',
      // 这份补查文档是"自己取的"：来源就是它自己，或者没有记录。
      sourceTasks: null,
      queryDomain: { coordinateSystem: 'bd09ll', origin: [116.404, 39.915], paddingMeters: 50,
        widened: false, envelopeLocalMeters: [-1300, -1300, 1300, 1300],
        polygonLocalMeters: [] },
      dataObtainedAt: CREATED_AT + 2, countsByCategory: { dining: 1, leisure: 2 },
      // 结果文档沿用 FacilityGroup 的形状；这里按"旧修订没记过首轮计划"来，读作 null。
      initialPlan: null,
      facilities: [], nearbyFacilities: [], reviewCandidates: [], excludedCandidates: [],
      quarantine: [], queryCoverage: [], queryIncompleteRegions: {},
      // 补查的结果文档不是"这次体检的达标判决"：达标比例属于任务自己的修订，
      // 所以这份夹具如实报"未记录"，而不是借一个 0 或 100% 来表达什么。
      queryAreaCoverage: null, statistics: {},
      warnings: [], stopReason: null },
    requests: 20, networkRequests: 20, budget: { limit: 60, spent: 20, remaining: 40 },
    issues: [], notes: ['扩展类别只做点位、数量和分类展示：不进入核心综合分。'],
    ...overrides,
  };
}

export function route(overrides: Partial<FacilityRoute> = {}): FacilityRoute {
  return {
    taskId: 'task-1', revision: 5, facilityId: 'synthetic:pharmacy-1', category: 'pharmacy',
    majorCategory: 'medical', origin: CENTER, destination: { lng: 116.41, lat: 39.92 },
    straightLineM: 700, withinRule: true, routeDistanceM: 805.05, accessDistanceM: 805.05,
    verificationLayer: 'strict', durationS: 670.87,
    observedDurationS: 670.87, poiStatus: 'verified_reachable', poiReason: null,
    evidenceGrade: 'verified', routeOrigin: CENTER, routeDestination: { lng: 116.41, lat: 39.92 },
    originOffsetM: 0, destinationOffsetM: 0, reason: null,
    provider: 'synthetic:checkup-routes', network: true, attempts: 1, budget: {}, notes: [],
    ...overrides,
  };
}

/** 合成的一份水系复核：范围、一段河道、一处补录、一处底图误绘、一处冲突与一座桥。 */
export function waterReview(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    reviewId: 'synthetic-river', version: '2026-09-01.1', label: 'synthetic-river@2026-09-01.1',
    title: '合成河段复核', reviewedAt: '2026-09-01', scope: '合成社区', appliesTo: {},
    sources: ['合成影像'], method: '合成', limitations: ['合成数据'],
    extent: polygon(),
    reaches: [{ osmId: 101, name: '合成河', widthM: 18, osmWidthTag: '10', status: 'verified',
      measurement: {}, baiduOffset: { minM: 23, medianM: 80, maxM: 193 }, note: '合成',
      geometry: polygon(0.001) }],
    crossings: [{ osmId: 202, highway: 'primary', river: '合成河', lengthM: 31.6,
      status: 'verified', note: '合成桥', anchor: { lng: 116.4, lat: 39.9 }, geometry: null }],
    supplements: [{ id: 'pond-a', status: 'verified', areaM2: 1200, sources: [], note: '合成池塘',
      anchor: { lng: 116.401, lat: 39.901 }, geometry: polygon(0.002) }],
    conflicts: [{ id: 'pond-b', status: 'unverified', areaM2: 660, sources: [], note: '来源矛盾',
      anchor: { lng: 116.402, lat: 39.902 }, geometry: polygon(0.003) }],
    basemapMisdrawn: [{ id: 'ghost-1', kind: 'river_displaced', verifiedAs: 'land', areaM2: 3100,
      note: '底图误绘', anchor: { lng: 116.403, lat: 39.903 }, geometry: polygon(0.004) }],
    ...overrides,
  };
}

export function water(overrides: Partial<WaterDataEvidence> = {}): WaterDataEvidence {
  return {
    obstacleLayerAvailable: true, osmDataVersion: 'synthetic-osm', sourcePbfSha256: 'sha-1',
    reviews: [waterReview()], rejectedReviews: [], domainAreaM2: AREA, reviewedAreaM2: AREA * 0.9,
    unreviewedAreaM2: AREA * 0.1, conflictAreaM2: 660,
    statements: ['水系障碍取自合成 OSM。'], ...overrides,
  };
}

/**
 * 一份结果的保留期状态（§5 B2 决策 2）。默认是"明细可用、还没到期、没有来源"——
 * 三个字段各自都是**一个事实**，不是占位：`expiresAt` 为 null 说的是"还没有能算出来的
 * 期限"（既没有会话租约、也还没到第三次后续体检），不是"永远不会到期"。
 */
export function retentionView(overrides: Partial<RetentionView> = {}): RetentionView {
  return { detailsAvailable: true, expiresAt: null, reason: null, sources: [], cleared: false,
    ...overrides };
}

/** 一个浏览会话此刻的状态：租约 300 秒、一个标签页、名下还没有体检。 */
export function sessionView(overrides: Partial<SessionView> = {}): SessionView {
  return { sessionId: 'session-1', tabId: 'tab-1', leaseSeconds: 300,
    expiresAt: SERVER_TIME + 300, resumed: false, openTabs: 1, tasks: 0, ...overrides };
}

/**
 * 明细到期之后的那一份视图（§5 B2 决策 2）。默认是"会话结束导致的到期"：
 * 结论与汇总都在，明细一个都没有 —— 这正是它要表达的状态。
 */
export function retainedView(overrides: Partial<RetainedCheckupView> = {}): RetainedCheckupView {
  return {
    taskId: 'task-1', revision: 5, stage: 'reporting', businessStatus: 'partial',
    resultHash: 'hash-5',
    summary: {
      facilitiesStatus: 'partial',
      facilities: { queryStatus: 'partial', stopReason: 'network_budget_exhausted',
        counts: { facilities: 3 },
        queryAreaCoverage: { status: 'unmet', target: 0.8, sharedCompletionRatio: 0.5 } },
      scores: { overall: { available: true, coverageLowerPct: 40, coverageUpperPct: 70,
        assessablePct: 88.5 } },
    },
    retention: retentionView({ detailsAvailable: false, reason: 'session_closed',
      expiresAt: STARTED_AT, cleared: true, sources: ['task-1'] }),
    notes: ['明细（设施名称、UID、地址与坐标）已按保留期到期不再提供；这里的结论、分数与汇总是到期前定稿的那一份，未重算。'],
    ...overrides,
  };
}
