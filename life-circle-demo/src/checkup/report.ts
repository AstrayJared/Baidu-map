/**
 * 报告视图模型：把一份冻结的修订翻译成界面能直接画的东西。
 *
 * 这个模块**只做翻译，不做判断**。覆盖率、面积、灰区范围、证据等级全部由后端算好，
 * 这里一个数也不重新推导 —— 判据只能有一处，前端再算一遍就会出现"图上写着 62%、面板
 * 写着 58%"这种没人能复核的分歧。唯一的例外是格式化（几位小数、用什么单位）。
 *
 * 因此这里的测试都在测同一件事：**不确定有没有被写成 0**。缺空间支持的类别没有百分比
 * （不是 0%），没有评估的灰区是"未评估"（不是"0 处"），未核验的设施是未知（不是"不可
 * 达"）。这些区分在数字上只差一个字，在结论上差一整句话。
 */
import type { CheckupSnapshot, CoverageRow, OverallScore, ReportVerification, ServiceZone,
  VerificationEvidence } from './contract';
import { majorMeta, subCategoryByKey } from '../taxonomy';

export const CATEGORY_ORDER = ['shopping', 'medical', 'education'] as const;

/** 三大类的名字与颜色在旧图层里也各有一份（`ApiMap.majorNames`、`FacilityPanel.groupNames`）。 */
export const CATEGORY_LABELS: Record<string, string> = {
  medical: '医疗健康', shopping: '购物消费', education: '教育',
  care: '疗养康养', dining: '餐饮', finance: '金融', public: '政务公共服务',
  leisure: '文体休闲', transport: '交通出行', life: '生活服务',
};
export const CATEGORY_COLORS: Record<string, string> = {
  shopping: '#12b886', medical: '#7b5cff', education: '#ff9f1a',
  care: '#d45d79', dining: '#ed7044', finance: '#3887c8', public: '#5368bf',
  leisure: '#9a63b6', transport: '#0a9d9c', life: '#9b714b',
};

export function reportDirectory(snapshot: CheckupSnapshot) {
  const frozen = snapshot.report?.categoryDirectory;
  if (frozen?.length) return [...frozen].sort((a, b) => a.order - b.order);
  // Old reports cannot tell us the complete request range. Only show recorded rows.
  return rows(snapshot).map((row, order) => ({ id: row.category,
    label: CATEGORY_LABELS[row.category] ?? row.category, order }));
}

/** 灰区理由码 → 中文。缺的码原样显示，宁可露出英文码也不猜它的意思。 */
const REASON_LABELS: Record<string, string> = {
  beyond_service_distance: '超出服务距离',
  water_data_conflict: '水系数据冲突（未裁决）',
  entrance_unresolved_nearby: '附近设施入口未核实',
  no_valid_entrance: '无可信入口',
  views_disagree: '两个视图结论不一致',
  graph_disconnected: '路网不连通',
  distance_in_tolerance_band: '距离落在容差带内',
  query_status: '设施检索未完成',
  query_incomplete_nearby: '附近设施检索未查完',
  verification_conflict: '实测路线与模型不一致（局部未决）',
  verified_route: '实测路线确认可达',
  no_legal_attachment: '无法接入路网',
  endpoint_mapping_unconfirmed: '路线端点未能严格对应请求的设施位置',
  endpoint_offset: '路线端点偏移超过容差，不能形成可靠判定',
  entrance_unresolved: '设施入口尚未解决，路线不能确认可达',
  task_budget_exhausted: '本任务路线预算已用尽',
  layer_budget_exhausted: '本层路线预算已用尽',
  daily_budget_exhausted: '当日请求额度已用尽',
  timeout: '路线请求超时',
  network_error: '路线请求失败',
};

/** 给不出分数的原因码 → 中文。 */
const UNAVAILABLE_LABELS: Record<string, string> = {
  no_spatial_support: '缺少该区域的步行路网或无法建立网格',
  osm_graph_unavailable: '步行路网不可用，无法建立覆盖评估',
  facility_stage_unavailable: '设施检索阶段未产出可用结果',
  facility_query_incomplete: '设施检索未完成，证据不足以评估覆盖率',
  assessment_domain_exceeds_grid_limit: '评估范围超过网格上限',
  area_partition_mismatch: '评估面积分区不一致，结果暂不可用',
  empty_assessment_domain: '评估范围为空',
  categories_not_analysed: '只评估了部分大类，不能加权成总体分',
  category_without_spatial_support: '有大类缺少空间支持，不能加权成总体分',
  query_incomplete: '设施检索未完成，暂无可用评估结果',
  no_usable_evidence: '无可用覆盖证据',
};

export function categoryLabel(category: string, snapshot?: CheckupSnapshot): string {
  const frozen = snapshot?.report?.categoryDirectory?.find(item => item.id === category);
  if (frozen) return frozen.label;
  return CATEGORY_LABELS[category]
    ?? majorMeta[category as keyof typeof majorMeta]?.label
    ?? subCategoryByKey[category]?.label
    ?? category;
}

export function reasonLabel(reason: string | null): string | null {
  if (reason === null || reason === '') return null;
  return REASON_LABELS[reason] ?? reason;
}

export function verificationLayerLabel(layer: string | null | undefined): string {
  return layer === 'strict' ? '严格确认' : layer === 'endpoint_tolerance'
    ? '含端点接入距离估计' : '未形成可用判定';
}

export function verificationFacilityRows(snapshot: CheckupSnapshot) {
  const records = snapshot.report?.verification.facilities ?? snapshot.verification?.facilities ?? [];
  return records.map(record => ({
    id: String(record.facilityId ?? '未知设施'),
    layer: verificationLayerLabel(record.verificationLayer as string | null),
    routeDistance: typeof record.routeDistanceM === 'number' ? `${record.routeDistanceM.toFixed(0)} 米` : '无有效路线',
    accessDistance: typeof record.accessDistanceM === 'number' ? `${record.accessDistanceM.toFixed(0)} 米` : '无法估计',
    originOffset: typeof record.originOffsetM === 'number' ? `${record.originOffsetM.toFixed(1)} 米` : '未知',
    destinationOffset: typeof record.destinationOffsetM === 'number' ? `${record.destinationOffsetM.toFixed(1)} 米` : '未知',
    entrance: record.entranceStatus === 'unresolved' ? '入口未解决'
      : typeof record.entranceStatus === 'string' ? record.entranceStatus : '入口状态未记录',
    reason: reasonLabel(typeof record.poiReason === 'string' ? record.poiReason
      : typeof record.reason === 'string' ? record.reason : null),
  }));
}

export function unavailableLabel(reason: string | null): string {
  if (reason === null || reason === '') return '本类别没有空间支持，未给出百分比';
  return UNAVAILABLE_LABELS[reason] ?? reason;
}

export function percent(value: number | null, digits = 1): string {
  return value === null ? '无法确定' : `${value.toFixed(digits)}%`;
}

/** 面积一律用平方米，超过一万改用"公顷"：读者要能一眼比大小，而不是数零。 */
export function area(value: number | null): string {
  if (value === null) return '无法确定';
  if (Math.abs(value) >= 10_000) return `${(value / 10_000).toFixed(2)} 公顷`;
  return `${value.toFixed(0)} m²`;
}

const ZONE_STATUS_LABELS: Record<string, string> = {
  complete: '评估完成', partial: '部分评估', failed: '评估未完成', absent: '未评估',
};

export type CoverageItem = {
  category: string;
  label: string;
  color: string;
  supported: boolean;
  lowerPct: number | null;
  upperPct: number | null;
  widthPct: number | null;
  assessablePct: number | null;
  unknownPct: number | null;
  /** 区间退化为一个点：三类面积都算清了，没有未知面积。 */
  degenerate: boolean;
  coveredM2: number | null;
  gapM2: number | null;
  unknownM2: number | null;
  evidenceGrade: string | null;
  unavailableReason: string | null;
  queryComplete: boolean | null;
};

/** 报告里那一节优先（它带着面积），没有报告时退回分数那一节。 */
function rows(snapshot: CheckupSnapshot): Array<CoverageRow | ScoreRowView> {
  const report = snapshot.report;
  if (report && report.categories.length > 0) return report.categories;
  return snapshot.scores?.categories ?? [];
}

type ScoreRowView = {
  category: string; supported: boolean | null; coverageLowerPct: number | null;
  coverageUpperPct: number | null; assessablePct: number | null; unknownPct: number | null;
  intervalWidthPct: number | null; intervalDegenerate: boolean | null;
  unavailableReason: string | null; coveredM2?: number | null; gapM2?: number | null;
  unknownM2?: number | null; evidenceGrade?: string;
};

/** 按固定顺序给出三类，报告里多出来的类别跟在后面，绝不因为顺序丢掉一行。 */
export function coverageItems(snapshot: CheckupSnapshot): CoverageItem[] {
  const byCategory = new Map<string, ScoreRowView>();
  for (const row of rows(snapshot)) byCategory.set(row.category, row as ScoreRowView);
  const directory = reportDirectory(snapshot);
  const order = [...directory.map(item => item.id),
    ...[...byCategory.keys()].filter(category => !directory.some(item => item.id === category))];
  return order.map(category => {
    const row = byCategory.get(category);
    // `supported` 缺省当"不支持"读：没有明确说支持就不给百分比。
    const supported = row?.supported === true;
    return {
      category, label: categoryLabel(category, snapshot),
      color: CATEGORY_COLORS[category] ?? '#8a94a6',
      supported,
      lowerPct: supported ? row?.coverageLowerPct ?? null : null,
      upperPct: supported ? row?.coverageUpperPct ?? null : null,
      widthPct: supported ? row?.intervalWidthPct ?? null : null,
      assessablePct: supported ? row?.assessablePct ?? null : null,
      unknownPct: supported ? row?.unknownPct ?? null : null,
      degenerate: supported && (row?.intervalDegenerate ?? false),
      coveredM2: row?.coveredM2 ?? null,
      gapM2: row?.gapM2 ?? null,
      unknownM2: row?.unknownM2 ?? null,
      evidenceGrade: row?.evidenceGrade ?? null,
      unavailableReason: supported ? null : row?.unavailableReason ?? (
        snapshot.facilities?.queryStatus !== 'completed'
          ? 'query_incomplete' : 'no_usable_evidence'),
      queryComplete: typeof snapshot.facilities?.statistics?.queryCompleteByMajor === 'object'
        && snapshot.facilities.statistics.queryCompleteByMajor !== null
        ? (snapshot.facilities.statistics.queryCompleteByMajor as Record<string, boolean>)[category] ?? null
        : snapshot.facilities ? snapshot.facilities.queryStatus === 'completed' : null,
    };
  });
}

/** 柱状图的一根柱：从下界画到上界。区间退化成点时柱长为零，界面必须另作说明。 */
export type CoverageBar = {
  category: string; label: string; color: string; lower: number; span: number; available: boolean;
};

export function coverageBars(items: CoverageItem[]): CoverageBar[] {
  return items.map(item => ({
    category: item.category, label: item.label, color: item.color,
    // 不支持的类别画成一根零长的柱子会被读成"0%"，所以这里给 0 长度并由 `available`
    // 标记让界面改成灰条 + "无法确定"。
    lower: item.lowerPct ?? 0,
    span: item.lowerPct === null || item.upperPct === null ? 0 : item.upperPct - item.lowerPct,
    available: item.lowerPct !== null && item.upperPct !== null,
  }));
}

/** 仅为报告排序；缺面积的旧版记录排在已知面积之后，不补零。 */
export function coverageGroups(items: CoverageItem[]) {
  return {
    assessed: items.filter(item => item.supported).sort((a, b) =>
      a.gapM2 === null ? (b.gapM2 === null ? 0 : 1)
        : b.gapM2 === null ? -1 : b.gapM2 - a.gapM2),
    unavailable: items.filter(item => !item.supported),
  };
}

export function reportSummary(snapshot: CheckupSnapshot) {
  const overall = overallView(snapshot);
  const gaps = gapSummary(snapshot);
  const verification = verificationView(snapshot);
  const { assessed } = coverageGroups(coverageItems(snapshot));
  const largest = assessed.find(item => item.gapM2 !== null && item.gapM2 > 0);
  return {
    coverage: overall.available
      ? `总体加权覆盖区间 ${percent(overall.lowerPct)} ～ ${percent(overall.upperPct)}。`
      : '总体覆盖暂无法评估，请查看分类结果及其限制。',
    gap: !gaps.assessed ? '服务盲区未评估，暂不能判断缺口。'
      : `已评估部分的服务盲区合计 ${gaps.gapAreaText}。${largest
        ? `分类缺口面积最大的是${largest.label}（${area(largest.gapM2)}）。` : ''}`,
    verification: verification.summary,
    limitations: evidenceNotes(snapshot).filter(note => note.level === 'warning'),
  };
}

export type RadarView = {
  available: boolean;
  indicators: Array<{ name: string; max: number }>;
  series: Array<{ name: string; values: number[] }>;
  missing: string[];
};

/** 雷达图：两条线是区间的上下界。任何一类缺下界就整张图不给 —— 补 0 会画成一个凹角。 */
export function radarView(items: CoverageItem[]): RadarView {
  const usable = items.filter(item => item.supported && item.lowerPct !== null && item.upperPct !== null);
  if (items.length === 0 || usable.length !== items.length) {
    return { available: false, indicators: [], series: [],
      missing: items.filter(item => !usable.includes(item)).map(item => item.label) };
  }
  return {
    available: true,
    indicators: items.map(item => ({ name: item.label, max: 100 })),
    series: [
      { name: '最低覆盖率', values: items.map(item => item.lowerPct as number) },
      { name: '最高覆盖率', values: items.map(item => item.upperPct as number) },
    ],
    missing: [],
  };
}

export type OverallView = {
  available: boolean;
  reason: string | null;
  missingCategories: string[];
  lowerPct: number | null;
  upperPct: number | null;
  assessablePct: number | null;
  unknownPct: number | null;
  weights: Record<string, number>;
  /** 权重是否等权。界面要把"每个大类各占 1/3"说出来，否则加权分看着像覆盖率。 */
  equalWeights: boolean;
};

export function overallView(snapshot: CheckupSnapshot): OverallView {
  const overall: OverallScore | null = snapshot.report?.overall ?? snapshot.scores?.overall ?? null;
  if (!overall || !overall.available) {
    return { available: false, reason: overall?.reason ?? 'scores_missing', missingCategories:
      (overall?.missingCategories ?? []).map(category => categoryLabel(category, snapshot)),
      lowerPct: null, upperPct: null,
      assessablePct: null, unknownPct: null, weights: {}, equalWeights: false };
  }
  const weights = Object.values(overall.weights ?? {});
  const equal = weights.length > 0 && weights.every(weight => Math.abs(weight - weights[0]) < 1e-9);
  return { available: true, reason: null, missingCategories: [], lowerPct: overall.coverageLowerPct,
    upperPct: overall.coverageUpperPct, assessablePct: overall.assessablePct,
    unknownPct: overall.unknownPct, weights: overall.weights ?? {}, equalWeights: equal };
}

export function overallUnavailableText(snapshot: CheckupSnapshot): string {
  const view = overallView(snapshot);
  const reason = view.reason === 'categories_not_analysed'
    ? '部分请求类别尚无可用评估结果，不能代表全部类别的总体覆盖。'
    : view.reason === 'category_without_spatial_support'
      ? '部分类别缺少空间支持，不能计算全部类别的总体覆盖。'
      : '评分阶段未产出全部类别的总体覆盖。';
  return `${reason}${view.missingCategories.length ? `涉及：${view.missingCategories.join('、')}。` : ''}`;
}

export type ZoneItem = {
  id: string;
  index: number;
  title: string;
  kind: string;
  kindLabel: string;
  categories: string[];
  categoryLabels: string[];
  areaM2: number;
  areaText: string;
  parts: number;
  evidenceGrade: string;
  queryStatus: string;
  reason: string | null;
  reasonLabel: string | null;
  nearestFacility: string | null;
  suggestion: string;
  /** 面积小于标注阈值：地图上不默认标注，但灰区本身仍然在。 */
  labelled: boolean;
};

/** 灰区只存最近设施 ID；若本次快照带设施目录，报告优先给读者看名称。 */
function facilityName(snapshot: CheckupSnapshot, id: string | null): string | null {
  if (id === null) return null;
  const record = (snapshot.facilities?.facilities ?? []).find(item => item.id === id);
  return typeof record?.name === 'string' && record.name.length > 0 ? record.name : id;
}

export const ZONE_KIND_LABELS: Record<string, string> = {
  single: '单类灰区', composite: '综合灰区',
};

export function zoneItems(snapshot: CheckupSnapshot): ZoneItem[] {
  const zones: ServiceZone[] = snapshot.report?.gaps.zones ?? snapshot.serviceGaps?.zones ?? [];
  return [...zones].sort((a, b) => b.areaM2 - a.areaM2).map((zone, position) => ({
    id: zone.id, index: position + 1,
    title: `灰区 ${position + 1}`,
    kind: zone.kind,
    kindLabel: ZONE_KIND_LABELS[zone.kind] ?? zone.kind,
    categories: zone.categories,
    categoryLabels: zone.categories.map(category => categoryLabel(category, snapshot)),
    areaM2: zone.areaM2, areaText: area(zone.areaM2), parts: zone.parts,
    evidenceGrade: zone.evidenceGrade, queryStatus: zone.queryStatus,
    reason: zone.reason, reasonLabel: reasonLabel(zone.reason),
    nearestFacility: facilityName(snapshot, zone.nearestFacility), suggestion: zone.suggestion,
    labelled: zone.labelVisible,
  }));
}

export type GapSummary = {
  /** 有没有做过服务覆盖评估。false 时下面的面积与条数一律不可读。 */
  assessed: boolean;
  status: string;
  statusLabel: string;
  reason: string | null;
  zones: ZoneItem[];
  gapAreaText: string | null;
  compositeAreaText: string | null;
  byCategory: Array<{ category: string; label: string; areaText: string }>;
  /** 面积小于标注阈值、地图上不标注的灰区数量。 */
  unlabelled: number;
  minLabelAreaM2: number | null;
  obstacleLayerAvailable: boolean | null;
  notes: string[];
};

export function gapSummary(snapshot: CheckupSnapshot): GapSummary {
  const gaps = snapshot.report?.gaps ?? snapshot.serviceGaps;
  const status = gaps?.status ?? 'absent';
  const zones = zoneItems(snapshot);
  const base: GapSummary = {
    assessed: false, status,
    statusLabel: ZONE_STATUS_LABELS[status] ?? status,
    // 只有报告那一节带 reason（快照的服务覆盖节没有这一栏）。
    reason: snapshot.report?.gaps.reason ?? null,
    zones,
    // 没有评估就没有面积可读：这里给 null 而不是 0，让界面只能说"未评估"。
    gapAreaText: null, compositeAreaText: null,
    byCategory: Object.entries(gaps?.byCategoryM2 ?? {})
      .map(([category, value]) => ({ category, label: categoryLabel(category), areaText: area(value) })),
    // 后端只报"哪些没标注"，阈值也由它给：前端不去自己比面积，免得两边阈值不一致。
    unlabelled: zones.filter(zone => !zone.labelled).length,
    minLabelAreaM2: gaps?.minLabelAreaM2 ?? null,
    obstacleLayerAvailable: gaps?.obstacleLayerAvailable ?? null,
    notes: gaps?.notes ?? [],
  };
  if (!gaps || status === 'failed') return base;
  return { ...base, assessed: true, gapAreaText: area(gaps.gapAreaM2),
    compositeAreaText: area(gaps.compositeAreaM2) };
}

export type VerificationView = {
  available: boolean;
  status: string;
  statusLabel: string;
  provider: string | null;
  checked: number;
  failed: number;
  unresolved: number;
  routeReturns: number;
  strictConfirmed: number;
  toleranceEstimated: number;
  noUsableDecision: number;
  reason: string | null;
  notes: string[];
  summary: string;
};

export function verificationView(snapshot: CheckupSnapshot): VerificationView {
  // 报告里那一节是冻结件，优先读它；两者读到的字段名一致，取并集即可。
  const verification: VerificationEvidence | ReportVerification | null =
    snapshot.report?.verification ?? snapshot.verification;
  const status = verification?.status ?? 'not_integrated';
  const statusLabel = { not_integrated: '未核验', complete: '核验完成', partial: '部分核验',
    failed: '核验未完成' }[status] ?? status;
  if (!verification || status === 'not_integrated') {
    return { available: false, status, statusLabel, provider: verification?.provider ?? null,
      checked: 0, failed: 0, unresolved: 0, routeReturns: 0, strictConfirmed: 0,
      toleranceEstimated: 0, noUsableDecision: 0,
      reason: verification?.reason ?? null, notes: [],
      summary: '本次体检未接入现实核验，设施结论的证据等级为模型推定。' };
  }
  const records = verification.facilities ?? [];
  const routeReturns = verification.routeReturns ?? records.filter(item => item.routeDistanceM !== null
    && item.routeDistanceM !== undefined).length;
  const strictConfirmed = verification.strictConfirmed ?? records.filter(item => item.verificationLayer === 'strict').length;
  const toleranceEstimated = verification.toleranceEstimated ?? records.filter(item => item.verificationLayer === 'endpoint_tolerance').length;
  const noUsableDecision = verification.noUsableDecision ?? records.filter(item => item.verificationLayer !== 'strict'
    && item.verificationLayer !== 'endpoint_tolerance').length;
  return {
    available: true, status, statusLabel, provider: verification.provider,
    checked: verification.checked, failed: verification.failed,
    routeReturns, strictConfirmed, toleranceEstimated, noUsableDecision,
    unresolved: verification.unresolved, reason: verification.reason,
    notes: verification.notes ?? [],
    summary: `已尝试核验 ${verification.checked} 处设施：${routeReturns} 条有效路线返回；`
      + `严格确认 ${strictConfirmed} 条、含端点接入距离估计 ${toleranceEstimated} 条、`
      + `未形成可用判定 ${noUsableDecision} 条。路线返回数与后三项有交叉，不能相加。`,
  };
}

export type Note = { key: string; level: 'info' | 'warning'; text: string };

/**
 * 证据说明。顺序固定（越靠前越影响结论），文本去重 —— 同一句限制说明在 limitations 和
 * notes 里各出现一次时只写一遍。
 */
export function evidenceNotes(snapshot: CheckupSnapshot): Note[] {
  const notes: Note[] = [];
  const push = (key: string, level: Note['level'], text: string) => {
    if (!text || notes.some(note => note.text === text)) return;
    // key 只用来给 React 认孩子，但一批提示可能共用同一个 code —— 一次 OSM 体检就有五条
    // ALGORITHM_WARNING，它们说的不是同一件事。重复的 key 会让 React 丢一条或重复一条，
    // 而这一段的意义正是"一条不少"，所以重名时补一个序号。
    const unique = notes.some(note => note.key === key) ? `${key}#${notes.length}` : key;
    notes.push({ key: unique, level, text });
  };
  const report = snapshot.report;
  push('rule', 'info', `判据：${snapshot.rules.metric === 'walking_route' ? '步行路线距离'
    : snapshot.rules.metric === 'straight_line' ? '直线距离' : '未确认的度量'}`
    + `，服务标准 ${snapshot.rules.threshold_m} 米`
    + (snapshot.rules.inclusive === false ? '（不含端点）' : ''));
  push('grid', 'info', snapshot.accessibility
    ? `网格 ${snapshot.accessibility.gridStepM} 米（细化 ${snapshot.accessibility.refinedStepM} 米），`
      + `搜索截止 ${snapshot.accessibility.searchCutoffM} 米，评估域 `
      + `${area(snapshot.accessibility.domainAreaM2)}。` : '');
  push('heatmap', 'info', snapshot.heatmap?.estimated
    ? '热力按网格内步行距离插值，是估计值，不是实测密度。' : '');
  push('obstacle', 'info', (report?.evidence.obstacleLayerAvailable ?? snapshot.serviceGaps
    ?.obstacleLayerAvailable) === false
    ? '未叠加障碍物图层：围墙、河道等阻断只通过路网连通性体现。' : '');
  push('catalog', 'info', report?.evidence.catalogCompleteness === 'unverified'
    ? '设施目录的完整性未经独立核实：目录里没有，不等于现实中不存在。' : '');
  push('legacy-directory', 'warning', report && !report.categoryDirectory?.length
    ? '旧报告未记录请求类别目录，无法确认当时完整的请求范围；这里只显示已记录类别。' : '');
  push('query', 'warning', report?.evidence.queryStatus && !['complete', 'completed'].includes(report.evidence.queryStatus)
    ? '本次设施检索未完全覆盖，相关灰区的结论可能是目录缺失造成的。' : '');
  const excluded = snapshot.accessibility?.excludedAreaM2 ?? 0;
  push('domain', 'warning', excluded > 0
    ? `评估域已排除 ${area(excluded)}（边界外或不可评估），报告只对评估域内的面积负责。` : '');
  // 水系证据的说明逐字照抄后端：来源、复核范围、底图误绘与冲突面积都在里面。
  for (const statement of snapshot.water?.statements ?? []) push('water', 'info', statement);
  for (const limitation of report?.limitations ?? []) push(`limit:${limitation}`, 'warning', limitation);
  for (const warning of snapshot.warnings ?? []) {
    push(`warning:${warning.code}`, warning.severity === 'error' ? 'warning' : 'info',
      `${warning.message}（${warning.scope}）`);
  }
  return notes;
}

export type WaterReviewSource = {
  label: string;
  title: string;
  reviewedAt: string | null;
  scope: string | null;
  sources: string[];
  method: string | null;
  limitations: string[];
  reaches: string[];
  crossings: number;
  conflicts: string[];
  misdrawn: number;
};

export type DataSourcesView = {
  /** false：这一版早于水系证据，说不出计算用了哪份水系。 */
  available: boolean;
  obstacle: string;
  reviews: WaterReviewSource[];
  areas: string | null;
  rejected: string[];
};

const texts = (value: unknown) => Array.isArray(value)
  ? value.filter((item): item is string => typeof item === 'string' && item.length > 0) : [];
const records = (value: unknown) => Array.isArray(value)
  ? value.filter((item): item is Record<string, unknown> => item !== null && typeof item === 'object') : [];
const optionalText = (value: unknown) => typeof value === 'string' && value.length > 0 ? value : null;
const tally = (value: unknown) => Array.isArray(value) ? value.length
  : typeof value === 'number' && Number.isFinite(value) ? value : 0;

/**
 * 数据来源栏：计算用的水系来自哪里、哪一版、复核了什么、哪里还没有结论。
 * 报告冻结的那一份优先；没有时读快照里的水系证据（两者出自同一次计算）。
 */
export function dataSourcesView(snapshot: CheckupSnapshot): DataSourcesView {
  const frozen = snapshot.report?.dataSources?.water;
  const water = (frozen && typeof frozen === 'object' ? frozen : snapshot.water) as
    Record<string, unknown> | null | undefined;
  if (!water) return { available: false, obstacle: '这一版早于水系证据：说不出计算用了哪一版水系。',
    reviews: [], areas: null, rejected: [] };
  const osm = optionalText(water.osmDataVersion);
  const sha = optionalText(water.sourcePbfSha256);
  const obstacle = water.obstacleLayerAvailable === false
    ? '水体障碍层不可用：这一版没有按水体判定。'
    : `水体障碍：OpenStreetMap${osm ? ` ${osm}` : '（版本未记录）'}${sha ? `，源文件 SHA-256 ${sha.slice(0, 12)}…` : ''}`
      + '；百度底图上的水面只作显示，不参与计算。';
  const reviews = records(water.reviews).map(review => ({
    label: optionalText(review.label) ?? '未命名复核',
    title: optionalText(review.title) ?? optionalText(review.label) ?? '未命名复核',
    reviewedAt: optionalText(review.reviewedAt), scope: optionalText(review.scope),
    sources: texts(review.sources), method: optionalText(review.method),
    limitations: texts(review.limitations),
    reaches: records(review.reaches).map(reach => [optionalText(reach.name) ?? `OSM ${reach.osmId}`,
      typeof reach.widthM === 'number' ? `按实测宽 ${reach.widthM} m 成面` : null,
      optionalText(reach.osmWidthTag) ? `OSM 原标宽 ${reach.osmWidthTag} m` : null]
      .filter(Boolean).join('，')),
    crossings: tally(review.crossings),
    conflicts: records(review.conflicts).map(item => [optionalText(item.id) ?? '未命名',
      typeof item.areaM2 === 'number' ? `约 ${Math.round(item.areaM2)} 平方米` : null,
      item.status === 'unverified' ? '未裁决' : optionalText(item.status),
      optionalText(item.note)].filter(Boolean).join('，')),
    misdrawn: tally(review.basemapMisdrawn),
  }));
  const reviewed = typeof water.reviewedAreaM2 === 'number' ? water.reviewedAreaM2 : null;
  const unreviewed = typeof water.unreviewedAreaM2 === 'number' ? water.unreviewedAreaM2 : null;
  const conflict = typeof water.conflictAreaM2 === 'number' ? water.conflictAreaM2 : null;
  const areas = reviews.length === 0 ? null
    : `评估域内：复核范围 ${area(reviewed)}，未复核 ${area(unreviewed)}（按 OSM 原样），`
      + `数据冲突／未知 ${area(conflict)}（不计覆盖、不计灰区）。`;
  return { available: true, obstacle, reviews, areas, rejected: texts(water.rejectedReviews) };
}

