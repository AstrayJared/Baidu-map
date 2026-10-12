/**
 * 运行时校验 v2 体检响应。
 *
 * 生成出来的 `contract.ts` 只在编译期存在：`fetch` 返回的是 `unknown`，把它断言成
 * `CheckupSnapshot` 只是把"没检查"写成了"已检查"。这一层是唯一真正读字段的地方，
 * 所以任何"这个响应能不能拿来渲染"的判断都放在这里。
 *
 * 校验只做一件事：**拒绝不自洽的文档，绝不补全或修正它**。几个不自洽是必须当场拦住的：
 *
 * - 修订号与所请求的那一版不符 —— 渲染旧修订会让人以为在看新结论；
 * - `accessibility.status === 'failed'` 却带着灰区或分数 —— 失败的评估不可能产出它们；
 * - 核验是 `not_integrated` 却在报告里写着 `available: true` —— "没有核验"会被读成
 *   "核验过、没发现问题"；
 * - 有空间支持的类别 `C + G + U` 与评估域 A 对不上 —— §11.3 的验收门槛，也是"分数被
 *   人工抬过"最容易留下的痕迹。
 *
 * 它不推导任何结论：设施是否可达、面积属于哪一态，都由后端说，这里只核对它说清了没有。
 */
import type { CheckupLayer, CheckupSnapshot, CheckupTaskView, FacilityExtensionDocument,
  FacilityExtensionView, FacilityRetryView, FacilityRoute, Origin, ReportEvidence, RetentionView,
  RetainedCheckupView, ServiceZone, SessionView, TaskProgress } from './contract';

type RecordValue = Record<string, unknown>;
const object = (value: unknown): value is RecordValue =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
// 这三个都要写成显式的类型谓词：箭头函数的返回值不会被推导成 `x is T`，推理成
// `boolean` 之后 `if (!count(x) || x < 1)` 里的 `x` 还是 `unknown`，编译期就少了一层保护。
const count = (value: unknown): value is number =>
  finite(value) && Number.isInteger(value) && value >= 0;
const text = (value: unknown): value is string => typeof value === 'string';
const nullableText = (value: unknown) => value === null || text(value);
const nullableNumber = (value: unknown) => value === null || finite(value);
const oneOf = <T extends string>(value: unknown, allowed: readonly T[]): value is T =>
  typeof value === 'string' && (allowed as readonly string[]).includes(value);

export const TASK_STATUSES = ['queued', 'running', 'cancelling', 'completed', 'failed', 'cancelled'] as const;
export const STAGES = ['isochrone', 'poi', 'accessibility', 'verification', 'reporting', 'ready'] as const;
export const LAYER_IDS = ['isochrone', 'facilities', 'accessibility', 'service_gaps', 'heatmap',
  'verification', 'report'] as const;
const BUSINESS = ['complete', 'partial', 'insufficient'] as const;
const EVIDENCE_STATUS = ['not_integrated', 'complete', 'partial', 'failed'] as const;

export type Stage = typeof STAGES[number];
export type LayerId = typeof LAYER_IDS[number];

function point(value: unknown): value is Origin {
  return object(value) && finite(value.lng) && finite(value.lat)
    && value.lng >= -180 && value.lng <= 180 && value.lat > -85 && value.lat < 85;
}

/** GeoJSON 面或非空：环至少四个点才闭合得起来，半张形状不能画。 */
function polygon(value: unknown): boolean {
  if (value === null) return true;
  if (!object(value) || !['Polygon', 'MultiPolygon'].includes(value.type as string)) return false;
  const rings = value.type === 'MultiPolygon' ? value.coordinates : [value.coordinates];
  return Array.isArray(rings) && rings.length > 0 && rings.every(ringSet =>
    Array.isArray(ringSet) && ringSet.length > 0 && ringSet.every(ring =>
      Array.isArray(ring) && ring.length >= 4 && ring.every(position =>
        Array.isArray(position) && position.length >= 2 && finite(position[0]) && finite(position[1]))));
}

function pointGeometry(value: unknown): boolean {
  return object(value) && value.type === 'Point' && Array.isArray(value.coordinates)
    && value.coordinates.length >= 2 && finite(value.coordinates[0]) && finite(value.coordinates[1]);
}

/** GeoJSON 面或空：灰区与评估域都必须是面，`null` 表示"这一层这次没有形状"。 */
function geometry(value: unknown): boolean {
  return polygon(value);
}

/**
 * 图层几何的三种形态。后端按组返回不同形态，客户端必须能分辨，否则设施、覆盖、灰区、
 * 热力、核验这五层会被当成"结构异常"整层丢掉 —— 而那正好是体检图上最要紧的五层。
 *
 * - `polygon`：等时圈这一层给的是单个面；
 * - `collection`：其余各层给的是要素集合（设施是点，灰区是面）；
 * - `document`：报告这一层没有形状，只有一份文档。
 *
 * "没有形状"与"空集合"都画不出东西，但含义不同，所以这里保留 `kind` 让调用方自己说。
 */
export type LayerGeometry =
  | { kind: 'polygon'; geometry: RecordValue }
  | { kind: 'collection'; features: RecordValue[]; properties: RecordValue }
  | { kind: 'document'; document: RecordValue }
  | { kind: 'none' };

/**
 * 校验与读取是同一步：能读出来的形状，一定已经过了校验；读不出来的一律返回 `null`，
 * 由调用方拒绝整层。**绝不部分接受** —— 一条缺了几何的要素被静默跳过，表现就是地图上
 * 少一个设施，而那正是体检报告最不能出的错。
 */
export function readLayerGeometry(layer: CheckupLayer): LayerGeometry | null {
  const value = layer.geometry;
  if (object(value) && value.type === 'FeatureCollection') {
    if (!Array.isArray(value.features)) return null;
    if (!value.features.every(feature => object(feature) && feature.type === 'Feature'
      && (pointGeometry(feature.geometry) || polygon(feature.geometry))
      && (feature.properties === undefined || object(feature.properties)))) return null;
    return { kind: 'collection', features: value.features as RecordValue[],
      properties: object(value.properties) ? value.properties : {} };
  }
  if (object(value) && (value.type === 'Polygon' || value.type === 'MultiPolygon')) {
    return polygon(value) ? { kind: 'polygon', geometry: value } : null;
  }
  // 认不出来的形状一律拒绝：一个既不是面也不是要素集合的字典画到图上，是凭空多出来的东西。
  if (object(value)) return null;
  if (object(layer.document)) return { kind: 'document', document: layer.document };
  // 既没有形状也没有文档：这是"这一层这次没有内容"（等时圈失败时就是这样），不是错误。
  return { kind: 'none' };
}

/**
 * 阶段内的一步。`count` 是这一步已经发生的事，`limit` 是它不会越过的数（预算、候选数），
 * 不是预计总量 —— 所以计数不许超过上限，而没有计数的一步也不该带单位。
 */
export function validProgress(value: unknown): value is TaskProgress {
  return object(value) && text(value.step) && value.step.length > 0
    && text(value.label) && value.label.length > 0
    && (value.count === null || count(value.count))
    && (value.limit === null || count(value.limit))
    && (value.count === null || value.limit === null || value.count <= value.limit)
    && nullableText(value.unit) && (value.count !== null || value.unit === null)
    && finite(value.since) && value.since > 0;
}

/** 服务端时间：缺席（旧版后端）可以，给了就必须是正的有限数。 */
const optionalTime = (value: unknown) => value === undefined || value === null || (finite(value) && value > 0);

/**
 * 运行中的计时字段。它们晚于任务视图的其余部分加入：旧版后端不给，界面就说"未记录"，
 * 而不是把整份任务视图判成结构异常。给了的必须彼此说得通 —— 都在同一台服务器的墙钟
 * 上，所以阶段与步骤不会早于任务开始，活动时间不会晚于服务端的"现在"。
 */
function validTiming(value: RecordValue): boolean {
  const { serverTime, startedAt, finishedAt, stageStartedAt, lastActivityAt, progress } = value;
  if (!(serverTime === undefined || (finite(serverTime) && serverTime > 0))) return false;
  if (![startedAt, finishedAt, stageStartedAt, lastActivityAt].every(optionalTime)) return false;
  if (!(progress === undefined || progress === null || validProgress(progress))) return false;
  const started = finite(startedAt) ? startedAt : null;
  const now = finite(serverTime) ? serverTime : null;
  // 同一台机器的墙钟，只留一点写库与序列化之间的余量。
  const SLACK = 1;
  if (started !== null && finite(finishedAt) && finishedAt < started - SLACK) return false;
  if (started !== null && finite(stageStartedAt) && stageStartedAt < started - SLACK) return false;
  if (now !== null && finite(lastActivityAt) && lastActivityAt > now + SLACK) return false;
  if (now !== null && started !== null && started > now + SLACK) return false;
  return true;
}

export function validTaskView(value: unknown): value is CheckupTaskView {
  return object(value) && text(value.taskId) && value.taskId.length > 0
    && text(value.clientRequestId) && text(value.engine) && value.engine.length > 0
    && oneOf(value.status, TASK_STATUSES)
    && (value.businessStatus === null || oneOf(value.businessStatus, BUSINESS))
    // 排队中的任务还没有阶段，终态之后也不再有：null 是"还没有"，不是缺失。
    && (value.stage === null || oneOf(value.stage, STAGES))
    && count(value.revision) && count(value.budget) && count(value.requests)
    && count(value.networkRequests) && finite(value.elapsedSeconds) && value.elapsedSeconds >= 0
    && finite(value.createdAt) && value.createdAt > 0
    && typeof value.cancelRequested === 'boolean'
    && nullableText(value.error)
    // 失败必须有话说：一个只说 "failed" 的任务视图没法告诉人下一步该做什么。
    && (value.status !== 'failed' || text(value.error))
    && validTiming(value) && validCompletion(value.completion);
}

export function validCompletion(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  return object(value) && ['reportRevision', 'roundNumber', 'roundPoiLimit', 'roundPoiRequests',
    'cumulativePoiRequests', 'routeRequests', 'routeRemaining', 'evaluatedCategories', 'totalCategories']
    .every(key => count(value[key]))
    && (value.roundPoiRequests as number) <= (value.roundPoiLimit as number)
    && (value.roundPoiLimit as number) > 0
    && (value.routeRequests as number) + (value.routeRemaining as number) <= 120
    && (value.evaluatedCategories as number) <= (value.totalCategories as number)
    && object(value.queryCompleteByMajor) && Object.values(value.queryCompleteByMajor).every(v => typeof v === 'boolean')
    && typeof value.canContinue === 'boolean' && typeof value.restartRetrieval === 'boolean'
    && nullableText(value.stopReason) && oneOf(value.evaluationStatus, ['partial', 'complete', 'limited'])
    && Array.isArray(value.limitations) && value.limitations.every(text);
}

function validZone(value: unknown): value is ServiceZone {
  return object(value) && text(value.id) && value.id.length > 0
    && count(value.index) && Array.isArray(value.categories) && value.categories.every(text)
    && text(value.kind) && finite(value.areaM2) && value.areaM2 >= 0 && count(value.parts)
    && Array.isArray(value.cellIds) && value.cellIds.every(text)
    && typeof value.labelVisible === 'boolean' && typeof value.suspected === 'boolean'
    && text(value.evidenceGrade) && text(value.queryStatus)
    && nullableText(value.nearestFacility) && nullableText(value.reason)
    && text(value.suggestion) && value.suggestion.length > 0
    && geometry(value.geometry) && value.geometrySystem === 'metric'
    && geometry(value.displayGeometry);
}

/** §11.3 的容差：`max(1 m², A×10⁻⁶)`。三类面积与评估域必须在它之内相符。 */
export function areaTolerance(domainAreaM2: number): number {
  return Math.max(1, Math.abs(domainAreaM2) * 1e-6);
}

function validReport(value: unknown): value is ReportEvidence {
  if (!object(value) || !text(value.reportId) || !finite(value.generatedAt)
    || !Array.isArray(value.categories) || !object(value.evidence)
    // 限制说明不能是空的：报告里每个数都是模型推出来的，一份"没有任何限制"的报告会被
    // 读成"这些结论就是事实"，而后端冻结的那一份从来不是空的（`LIMITATIONS`）。
    || !Array.isArray(value.limitations) || value.limitations.length === 0
    || !value.limitations.every(text) || !object(value.gaps) || !object(value.verification)) return false;
  if (!validCompletion(value.completion)) return false;
  const domain = value.domainAreaM2;
  if (domain !== null && !finite(domain)) return false;
  for (const row of value.categories) {
    if (!object(row) || !text(row.category) || !text(row.evidenceGrade)) return false;
    for (const key of ['coveredM2', 'gapM2', 'unknownM2', 'coverageLowerPct', 'coverageUpperPct',
      'assessablePct', 'unknownPct', 'intervalWidthPct'])
      if (!nullableNumber(row[key])) return false;
    if (row.supported === true && domain !== null) {
      const areas = [row.coveredM2, row.gapM2, row.unknownM2];
      if (!areas.every(finite)) return false;
      // C + G + U = A。对不上就不渲染：一个和评估域对不上的面积三元组，任何百分比都
      // 可能是从被改过的分母里算出来的。
      const total = (areas[0] as number) + (areas[1] as number) + (areas[2] as number);
      if (Math.abs(total - (domain as number)) > areaTolerance(domain as number)) return false;
    }
  }
  if (!Array.isArray(value.gaps.zones) || !value.gaps.zones.every(validZone)) return false;
  if (typeof value.gaps.obstacleLayerAvailable !== 'boolean') return false;
  if (typeof value.verification.available !== 'boolean' || !text(value.verification.status)) return false;
  // 没有核验服务却报"核验可用"是这一层最需要拦住的一种自相矛盾。
  if (value.verification.status === 'not_integrated' && value.verification.available !== false) return false;
  if (value.verification.available === true && value.verification.status === 'not_integrated') return false;
  if (!Array.isArray(value.verification.facilities)) return false;
  return true;
}

const WATER_ITEMS = ['reaches', 'supplements', 'conflicts', 'basemapMisdrawn', 'crossings'] as const;

/**
 * 水系证据：面积非负、说明是文字、每份复核里的几何要么没有要么是完整的面。一块画不出来
 * 的冲突面比没有更糟 —— 图上少了"数据冲突／未知"，读者就会把那里的底图水面当成已核实。
 */
function validWater(value: unknown): boolean {
  if (!object(value) || typeof value.obstacleLayerAvailable !== 'boolean'
    || !nullableText(value.osmDataVersion) || !Array.isArray(value.statements)
    || !value.statements.every(text) || !Array.isArray(value.rejectedReviews)
    || !value.rejectedReviews.every(text) || !Array.isArray(value.reviews)) return false;
  for (const key of ['reviewedAreaM2', 'conflictAreaM2'])
    if (!finite(value[key]) || (value[key] as number) < 0) return false;
  for (const key of ['domainAreaM2', 'unreviewedAreaM2'])
    if (value[key] !== null && (!finite(value[key]) || (value[key] as number) < 0)) return false;
  return value.reviews.every(review => object(review) && text(review.label)
    && (review.extent === undefined || polygon(review.extent))
    && WATER_ITEMS.every(key => review[key] === undefined || (Array.isArray(review[key])
      && (review[key] as unknown[]).every(item => object(item)
        && (item.geometry === undefined || polygon(item.geometry))))));
}

function validVerification(value: unknown): boolean {
  return object(value) && oneOf(value.status, EVIDENCE_STATUS) && nullableText(value.reason)
    && count(value.checked) && count(value.failed) && count(value.unresolved)
    && Array.isArray(value.facilities) && Array.isArray(value.conflicts)
    && object(value.queries) && Array.isArray(value.notes) && value.notes.every(text);
}

export function validSnapshot(value: unknown): value is CheckupSnapshot {
  if (!object(value) || value.schemaVersion !== 'checkup-v1' || !text(value.taskId)) return false;
  if (!validCompletion(value.completion)) return false;
  const revision = value.revision;
  if (!count(revision) || revision < 1 || !finite(value.generatedAt)
    || !point(value.center) || value.coordinateSystem !== 'bd09ll'
    || !oneOf(value.stage, STAGES) || !oneOf(value.businessStatus, BUSINESS)
    || !object(value.engine) || !object(value.isochrone) || !object(value.rules)
    || !object(value.scope) || !object(value.trace) || !text(value.trace.resultHash)
    || !oneOf(value.facilitiesStatus, EVIDENCE_STATUS) || !Array.isArray(value.warnings)) return false;
  const accessibility = value.accessibility;
  if (accessibility !== null) {
    if (!object(accessibility) || !oneOf(accessibility.status, ['complete', 'partial', 'failed'])
      || !Array.isArray(accessibility.categories)) return false;
  }
  // 可达性没跑成就不可能有灰区、热力或分数：它们全都是它算出来的。
  if ((accessibility === null || (object(accessibility) && accessibility.status === 'failed'))
    && (value.serviceGaps !== null || value.scores !== null)) return false;
  if (value.serviceGaps !== null && (!object(value.serviceGaps)
    || !oneOf(value.serviceGaps.status, ['complete', 'partial', 'failed'])
    || !Array.isArray(value.serviceGaps.zones) || !value.serviceGaps.zones.every(validZone)
    || typeof value.serviceGaps.obstacleLayerAvailable !== 'boolean')) return false;
  if (value.heatmap !== null && (!object(value.heatmap) || !object(value.heatmap.categories))) return false;
  if (value.scores !== null && (!object(value.scores) || !Array.isArray(value.scores.categories))) return false;
  if (value.verification !== null && !validVerification(value.verification)) return false;
  if (value.report !== null && !validReport(value.report)) return false;
  if (value.water !== undefined && value.water !== null && !validWater(value.water)) return false;
  // 重算来源只能是更早的一版：指向自己或之后的版本，说明修订链被改乱了。
  const recomputed = value.trace.recomputed;
  if (recomputed !== undefined && recomputed !== null && (!object(recomputed)
    || !count(recomputed.fromRevision) || recomputed.fromRevision >= revision)) return false;
  // 报告一出现，"报告里那一节"和"快照里那一节"必须是同一件事：报告冻结的是同一版
  // 结论，两处给出不同的可用性只能说明有一处被改过。
  if (object(value.report) && object(value.verification) && object(value.report.verification)) {
    const report = value.report.verification as RecordValue;
    if (typeof report.available === 'boolean'
      && (value.verification.status === 'not_integrated') !== (report.available === false)) return false;
  }
  return true;
}

export type EngineOption = {
  engineId: string;
  label: string;
  engineVersion: string;
  /** 引擎接受哪些预算档。界面只在这里取值，绝不自己编一档发过去（§4.1：不支持即 422）。 */
  budgets: number[];
  defaultBudget: number;
  requiresOsmGraph: boolean;
  notes: string[];
};

export type Capabilities = {
  schemaVersion: string;
  engines: EngineOption[];
  quota: RecordValue;
  budgets: RecordValue;
  coverage: RecordValue;
  rules: RecordValue;
  /** 当前部署采用的水系复核；旧后端不给这一项。条目由 water.ts 逐条认。 */
  waterReviews?: RecordValue[];
  categoryDirectoryVersion?: string;
  categoryDirectory?: Array<{ id: string; label: string; order: number }>;
  /** §3.4 的跨任务复用窗口；旧后端不给这一项。 */
  cache?: RecordValue;
  /**
   * 检索计划口径：本地处理上限、网络额度是否与它分开、首轮计划是否预留。旧后端不给这一项，
   * 界面缺了就不说 —— 缺项不是 0，也不是"未配置"。
   */
  poiPlanning?: RecordValue;
  /** 设施目录的 v2 视图：类别选择器与预算算术的唯一来源；旧后端不给这一项。 */
  facilityCategories?: RecordValue;
};

/** 能力表：界面靠它决定"能选什么"，所以引擎字段错一个就整份拒绝，不做部分接受。 */
export function validCapabilities(value: unknown): value is Capabilities {
  if (!object(value) || !text(value.schemaVersion) || !Array.isArray(value.engines)
    || value.engines.length === 0) return false;
  for (const engine of value.engines) {
    if (!object(engine) || !text(engine.engineId) || engine.engineId.length === 0
      || !text(engine.label) || !text(engine.engineVersion)
      || !Array.isArray(engine.budgets) || engine.budgets.length === 0
      || !engine.budgets.every(budget => count(budget) && budget > 0)
      || !count(engine.defaultBudget) || !engine.budgets.includes(engine.defaultBudget)
      || typeof engine.requiresOsmGraph !== 'boolean') return false;
  }
  if (value.waterReviews !== undefined && !Array.isArray(value.waterReviews)) return false;
  // 这几项是可选的，但给了就不能是别的形状：读成 undefined 由选择器说"后端没报这一项"，
  // 读成一个数组则会静默变成"没有可选类别"。
  if (value.cache !== undefined && !object(value.cache)) return false;
  if (value.facilityCategories !== undefined && !object(value.facilityCategories)) return false;
  if (value.poiPlanning !== undefined && !object(value.poiPlanning)) return false;
  return object(value.quota) && object(value.budgets) && object(value.coverage)
    && object(value.rules);
}

export function validLayer(value: unknown, layerId: LayerId): value is CheckupLayer {
  if (!object(value) || value.layerId !== layerId) return false;
  const revision = value.revision;
  // 几何的形态判据只有 `readLayerGeometry` 一处：能读出来的才算合法，读不出来的一律拒绝。
  if (readLayerGeometry(value as CheckupLayer) === null) return false;
  return count(revision) && revision >= 1
    && text(value.resultHash) && value.resultHash.length > 0
    && geometry(value.displayGeometry)
    && (value.document === null || object(value.document));
}

const POI_STATUS = ['pending', 'verified_reachable', 'verified_unreachable'] as const;

export function validFacilityRoute(value: unknown): value is FacilityRoute {
  if (!object(value) || !text(value.taskId)) return false;
  const revision = value.revision;
  if (!count(revision) || revision < 1
    || !text(value.facilityId) || !text(value.category) || !point(value.origin)
    || !point(value.destination) || !oneOf(value.poiStatus, POI_STATUS)
    || !nullableText(value.poiReason) || !oneOf(value.evidenceGrade, ['verified', 'model'])
    || !nullableNumber(value.straightLineM) || !nullableNumber(value.routeDistanceM)
    || !nullableNumber(value.durationS) || !nullableNumber(value.observedDurationS)
    || !(value.withinRule === null || typeof value.withinRule === 'boolean')
    || !text(value.provider) || typeof value.network !== 'boolean' || !count(value.attempts)) return false;
  // 两个字段是端点容差层带来的；旧后端没有它们，照样接受。
  if (value.accessDistanceM !== undefined && !nullableNumber(value.accessDistanceM)) return false;
  if (value.verificationLayer !== undefined
    && !(value.verificationLayer === null || oneOf(value.verificationLayer, ['strict', 'endpoint_tolerance'])))
    return false;
  // 有判定的路线一定报出了它所依据的距离；反过来不成立：误差带里、或证据层不成立时，
  // 后端报出距离但不下判定——界面照样不自己判 1000 米，判据仍只有后端一处。
  if (value.withinRule === null) return true;
  // accessDistanceM 缺席（旧后端）可以；在场就必须是判定所依据的那个数。
  return value.routeDistanceM !== null && value.accessDistanceM !== null;
}

/** 一次按需补查的状态：它自己的标识、预算和终态，与原任务的修订互不影响。 */
const EXTENSION_STATUS = ['queued', 'running', 'completed', 'partial', 'failed', 'cancelled'] as const;
const EXTENSION_STAGES = ['poi', 'ready'] as const;

export function validFacilityExtensionView(value: unknown): value is FacilityExtensionView {
  return object(value)
    && text(value.extensionId) && value.extensionId.length > 0
    && text(value.taskId) && value.taskId.length > 0
    && text(value.clientRequestId) && value.clientRequestId.length > 0
    && count(value.baseRevision) && value.baseRevision >= 1
    && oneOf(value.status, EXTENSION_STATUS)
    // 排队时还没有阶段：null 是"还没有"，不是缺失。
    && (value.stage === null || oneOf(value.stage, EXTENSION_STAGES))
    && Array.isArray(value.categories) && value.categories.length > 0 && value.categories.every(text)
    && object(value.budget) && count(value.requests) && count(value.networkRequests)
    && nullableText(value.facilitiesStatus) && nullableText(value.error)
    && object(value.countsByCategory) && finite(value.createdAt) && value.createdAt > 0
    && nullableNumber(value.finishedAt)
    // 失败必须有话说：只说 "failed" 的补查没法告诉人下一步做什么。
    && (value.status !== 'failed' || text(value.error));
}

/**
 * 一个浏览会话此刻的状态（§5 B2 决策 2）。``expiresAt`` 是租约到期时刻：过了它，这个
 * 会话的明细就不再保留。它不是"数据已经删了"，所以界面用它说明"什么时候会到期"，
 * 而不是拿它当"现在还能不能看"的判据 —— 那个问题由任务视图上的 ``retention`` 回答。
 */
export function validSessionView(value: unknown): value is SessionView {
  return object(value)
    && text(value.sessionId) && value.sessionId.length > 0
    && text(value.tabId) && value.tabId.length > 0
    && finite(value.leaseSeconds) && value.leaseSeconds > 0
    && finite(value.expiresAt) && value.expiresAt > 0
    && typeof value.resumed === 'boolean'
    && count(value.openTabs) && count(value.tasks);
}

/**
 * 明细到期之后仍然给得出的那一部分：结论、汇总与到期原因。
 *
 * 校验它存在的理由是**不许把"没有汇总"读成"这次没有结论"**：``summary`` 必须是对象，
 * 而 ``revision`` 必须是真的一版 —— 一份没有版本的"保留结果"没法与任何一次体检对上。
 */
export function validRetainedCheckupView(value: unknown): value is RetainedCheckupView {
  return object(value)
    && text(value.taskId) && value.taskId.length > 0
    && count(value.revision) && value.revision >= 1
    && oneOf(value.stage, STAGES)
    && oneOf(value.businessStatus, BUSINESS)
    && text(value.resultHash) && value.resultHash.length > 0
    && object(value.summary)
    && validRetentionView(value.retention)
    && Array.isArray(value.notes) && value.notes.every(text);
}

/** 任务视图上的保留期：明细还能不能提供、为什么、什么时候到期。 */
export function validRetentionView(value: unknown): value is RetentionView {
  if (!object(value) || typeof value.detailsAvailable !== 'boolean') return false;
  if (!nullableNumber(value.expiresAt)) return false;
  if (!(value.reason === null || oneOf(value.reason,
    ['session_closed', 'superseded', 'legacy', 'cleared'] as const))) return false;
  if (!Array.isArray(value.sources) || !value.sources.every(text)) return false;
  if (typeof value.cleared !== 'boolean') return false;
  // 明细还能提供时不该同时给一个"已经到期的原因"：两者放在一起就是一份自相矛盾的状态。
  return value.detailsAvailable ? value.reason === null : true;
}

/**
 * 一次重试的状态。它**不报**"发布了第几版"：那一版是任务自己的最新修订，客户端照常读
 * 任务即可 —— 在这里再存一份修订号，只会多出一份可能与任务不一致的副本。
 *
 * 与补查的区别在这里也是可见的：补查报的是它自己的结果文档，重试报的是"这次体检的检索
 * 有没有查完"，所以它必须带 `facilitiesStatus` 与停止原因，否则界面只能显示"跑完了"。
 */
export function validFacilityRetryView(value: unknown): value is FacilityRetryView {
  return object(value)
    && text(value.retryId) && value.retryId.length > 0
    && text(value.taskId) && value.taskId.length > 0
    && text(value.clientRequestId) && value.clientRequestId.length > 0
    && count(value.baseRevision) && value.baseRevision >= 1
    && oneOf(value.status, EXTENSION_STATUS)
    // 排队时还没有阶段：null 是"还没有"，不是缺失。
    && (value.stage === null || oneOf(value.stage, EXTENSION_STAGES))
    && object(value.budget) && count(value.requests) && count(value.networkRequests)
    && nullableText(value.facilitiesStatus) && nullableText(value.stopReason)
    && nullableText(value.error) && nullableNumber(value.finishedAt)
    && finite(value.createdAt) && value.createdAt > 0
    && (value.initialPlan === null || object(value.initialPlan))
    // 失败必须有话说：只说 "failed" 的重试没法告诉人下一步做什么。
    && (value.status !== 'failed' || text(value.error));
}

/**
 * 一次补查的完整结果。``group`` 为 null 只有一个意思：这次检索没有产出可用结果，
 * 原因写在 ``issues`` 里。它绝不是"这片区域没有这类设施"—— 空清单与空目录是两件事。
 */
export function validFacilityExtensionDocument(value: unknown): value is FacilityExtensionDocument {
  if (!object(value) || !text(value.extensionId) || !text(value.taskId)) return false;
  if (!count(value.baseRevision) || value.baseRevision < 1) return false;
  if (!Array.isArray(value.categories) || value.categories.length === 0
    || !value.categories.every(text)) return false;
  if (!oneOf(value.status, EXTENSION_STATUS)) return false;
  if (!nullableText(value.facilitiesStatus)) return false;
  if (!count(value.requests) || !count(value.networkRequests) || !object(value.budget)) return false;
  if (!Array.isArray(value.issues) || !Array.isArray(value.notes)) return false;
  // group 只允许是对象或 null：一个空的 {} 会被读成"查到了东西"，别的形状会被读成设施清单。
  if (value.group !== null && !object(value.group)) return false;
  // 结果文档必须自洽：说"有结果"就要是一个真出过结果的终态，说"没有结果"就必须给得出原因，
  // 否则界面只能显示一个既没设施也没解释的空面板。
  if (value.group !== null) return oneOf(value.status, ['completed', 'partial', 'cancelled']);
  return value.issues.length > 0;
}
