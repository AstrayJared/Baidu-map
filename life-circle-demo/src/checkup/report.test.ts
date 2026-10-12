/**
 * 视图模型测的是**"不知道"有没有被写成"0"**。
 *
 * 这块代码不重算任何数，所以遍历它的正确性没有意义；有意义的是它在缺数据时的措辞：
 * 缺空间支持的类别没有百分比（不是 0%），没做过评估的灰区面积是"未评估"（不是"0 m²"），
 * 未接入核验的设施是未知（不是"不可达"）。这三处一旦写成 0，报告就从"没查到"变成了
 * "查过，没有"，而读者不会去区分。
 */
import { describe, expect, it } from 'vitest';
import {
  area, coverageBars, coverageItems, dataSourcesView, evidenceNotes, gapSummary, overallView, percent,
  radarView, reasonLabel, unavailableLabel, categoryLabel, verificationView, verificationFacilityRows, zoneItems,
  coverageGroups, reportSummary,
} from './report';
import { AREA, CATEGORIES, report, snapshot, water, zone } from './fixtures';
import type { CheckupSnapshot } from './contract';

/** 把可达性阶段整段拿掉，模拟"没跑成"的修订。 */
function notAssessed(): CheckupSnapshot {
  const base = snapshot();
  return { ...base, accessibility: null, accessibilityStatus: 'failed', serviceGaps: null,
    scores: null, heatmap: null, report: null };
}

describe('formatting', () => {
  it('says "无法确定" instead of printing a null', () => {
    expect(percent(null)).toBe('无法确定');
    expect(area(null)).toBe('无法确定');
    expect(percent(40)).toBe('40.0%');
  });

  it('switches to hectares once the number stops being readable', () => {
    expect(area(12_500)).toBe('1.25 公顷');
    expect(area(2_500)).toBe('2500 m²');
  });

  it('translates extension major categories and minor category keys before rendering', () => {
    expect(categoryLabel('finance')).toBe('金融');
    expect(categoryLabel('public')).toBe('政务公共服务');
    expect(categoryLabel('bank')).toBe('银行');
    expect(categoryLabel('new_category')).toBe('new_category');
  });

  it('translates unavailable coverage reasons instead of exposing backend codes', () => {
    expect(unavailableLabel('facility_query_incomplete'))
      .toBe('设施检索未完成，证据不足以评估覆盖率');
    expect(unavailableLabel(null)).toBe('本类别没有空间支持，未给出百分比');
  });
});

describe('coverage rows', () => {
  it('shows every frozen requested category while leaving four without invented scores', () => {
    const base = snapshot();
    const ids = ['medical', 'shopping', 'education', 'care', 'dining', 'finance', 'public',
      'leisure', 'transport', 'life'];
    const frozen = report({ categoryDirectoryVersion: 'test-v1',
      categoryDirectory: ids.map((id, order) => ({ id, label: `类别${order}`, order })),
      categories: ids.slice(0, 6).map((id, index) => ({
        ...report().categories[index % report().categories.length], category: id })) });
    const items = coverageItems({ ...base, report: frozen });
    expect(items).toHaveLength(10);
    expect(coverageGroups(items).unavailable).toHaveLength(4);
    expect(items.find(item => item.category === 'life')).toMatchObject({
      supported: false, lowerPct: null, gapM2: null, label: '类别9' });
  });

  it('treats complete and completed query states as finished', () => {
    for (const queryStatus of ['complete', 'completed'] as const) {
      const base = snapshot();
      const old = report({ evidence: { ...report().evidence, queryStatus } });
      expect(evidenceNotes({ ...base, report: old }).some(note => note.key === 'query')).toBe(false);
    }
  });
  it('reads the interval from the report and keeps the areas beside it', () => {
    const items = coverageItems(snapshot());
    expect(items.map(item => item.category)).toEqual([...CATEGORIES]);
    expect(items[0]).toMatchObject({ label: '购物消费', lowerPct: 40, upperPct: 70, gapM2: AREA * 0.2,
      evidenceGrade: 'model' });
  });

  it('gives no percentage at all to a category without spatial support', () => {
    // 缺图时后端给的是"无法确定"：这里要是把 null 换成 0，报告就变成了"这一类一点都没有"。
    const base = snapshot();
    const broken = { ...base, report: report({ categories: [
      { ...report().categories[0], supported: false, coverageLowerPct: null, coverageUpperPct: null,
        intervalWidthPct: null, assessablePct: null, unknownPct: null,
        unavailableReason: 'no_spatial_support' }] }) };
    const item = coverageItems(broken)[0];
    expect(item.supported).toBe(false);
    expect(item.lowerPct).toBeNull();
    expect(coverageBars([item])[0]).toMatchObject({ lower: 0, span: 0, available: false });
  });

  it('keeps a category the report adds beyond the three majors', () => {
    const base = snapshot();
    const extra = { ...base, report: report({ categories: [
      ...report().categories, { ...report().categories[0], category: 'culture' }] }) };
    expect(coverageItems(extra).map(item => item.category)).toEqual([...CATEGORIES, 'culture']);
  });

  it('falls back to the score rows when the report is not published yet', () => {
    const early = snapshot({ report: null });
    expect(coverageItems(early)).toHaveLength(3);
    // 没有报告就没有面积：面积栏是"无法确定"，而不是 0。
    expect(coverageItems(early)[0].coveredM2).toBeNull();
  });

  it('draws the bar from the lower bound, spanning to the upper one', () => {
    expect(coverageBars(coverageItems(snapshot()))[0]).toMatchObject({ lower: 40, span: 30,
      available: true, color: '#12b886' });
  });
});

describe('radar', () => {
  it('draws two lines, one per bound', () => {
    const view = radarView(coverageItems(snapshot()));
    expect(view.available).toBe(true);
    expect(view.series.map(series => series.name)).toEqual(['最低覆盖率', '最高覆盖率']);
    expect(view.series[0].values).toEqual([40, 40, 40]);
  });

  it('refuses the whole chart rather than filling a missing category with zero', () => {
    const base = snapshot();
    const broken = { ...base, report: report({ categories: [
      { ...report().categories[0], coverageLowerPct: null, coverageUpperPct: null }] }) };
    const view = radarView(coverageItems(broken));
    expect(view.available).toBe(false);
    expect(view.missing).toContain('购物消费');
  });
});

describe('overall', () => {
  it('reports the weighted interval and says the weights are equal', () => {
    expect(overallView(snapshot())).toMatchObject({ available: true, lowerPct: 40, upperPct: 70,
      equalWeights: true });
  });

  it('gives no overall number when a category is missing', () => {
    const base = snapshot();
    const partial = { ...base, report: null, scores: { ...base.scores!, overall: { available: false,
      reason: 'category_without_spatial_support', missingCategories: ['medical'],
      coverageLowerPct: null, coverageUpperPct: null, assessablePct: null, unknownPct: null,
      categories: [], weights: {} } } };
    const view = overallView(partial);
    expect(view.available).toBe(false);
    expect(view.lowerPct).toBeNull();
    // 缺的那一类要说名字：只写"数据不足"没人知道该去补哪一类。
    expect(view.missingCategories).toEqual(['医疗健康']);
  });
});

describe('report conclusions', () => {
  it('uses the frozen overall in both summary and body when snapshot scores differ', () => {
    const base = snapshot();
    const value = snapshot({ scores: { ...base.scores!, overall: {
      ...base.scores!.overall!, coverageLowerPct: 99 } } });
    expect(overallView(value).lowerPct).toBe(40);
    expect(reportSummary(value).coverage).toContain('40.0% ～ 70.0%');
  });

  it('orders known gaps first without assigning zero to missing areas or unsupported categories', () => {
    const items = coverageItems(snapshot());
    const input = [{ ...items[0], gapM2: null }, { ...items[1], gapM2: 50 },
      { ...items[2], supported: false, gapM2: 100 }];
    const groups = coverageGroups(input);
    expect(groups.assessed.map(item => item.category)).toEqual(['medical', 'shopping']);
    expect(groups.unavailable.map(item => item.category)).toEqual(['education']);
    expect(input[0].gapM2).toBeNull();
  });

  it('does not report zero gaps when assessment is missing or failed', () => {
    expect(reportSummary(notAssessed()).gap).toContain('未评估');
    const value = snapshot({ report: report({ gaps: { ...report().gaps, status: 'failed' } }) });
    expect(gapSummary(value).assessed).toBe(false);
    expect(reportSummary(value).gap).toContain('未评估');
    expect(reportSummary(value).gap).not.toContain('0 m²');
  });

  it('keeps incomplete-query limitations beside partial conclusions', () => {
    const value = snapshot({ report: report({ evidence: { ...report().evidence, queryStatus: 'partial' } }) });
    expect(reportSummary(value).limitations.some(note => note.text.includes('设施检索未完全覆盖'))).toBe(true);
    expect(reportSummary(value).gap).toContain('已评估部分');
  });

  it('does not turn unverified results into verified conclusions', () => {
    const value = snapshot({ report: report({ verification: { ...report().verification,
      status: 'not_integrated', available: false } }) });
    expect(reportSummary(value).verification).toContain('模型推定');
  });
});

describe('grey zones', () => {
  it('lists the largest zone first and keeps the backend wording', () => {
    const base = snapshot();
    const zones = [zone({ id: 'a', areaM2: 1_000, suggestion: '小片' }),
      zone({ id: 'b', areaM2: 90_000, suggestion: '大片' })];
    const items = zoneItems({ ...base, report: report({ gaps: { ...report().gaps, zones } }) });
    expect(items.map(item => item.id)).toEqual(['b', 'a']);
    expect(items[0]).toMatchObject({ title: '灰区 1', kindLabel: '单类灰区', areaText: '9.00 公顷',
      suggestion: '大片', categoryLabels: ['医疗健康'] });
  });

  it('uses the facility name from the frozen directory for the nearest facility', () => {
    const base = snapshot({ facilities: {
      facilities: [{ id: 'synthetic:pharmacy-1', name: '社区药房' }],
    } as unknown as NonNullable<CheckupSnapshot['facilities']> });
    expect(zoneItems(base)[0].nearestFacility).toBe('社区药房');
  });

  it('translates a known reason and leaves an unknown one as the raw code', () => {
    const base = snapshot();
    const items = zoneItems({ ...base, report: report({ gaps: { ...report().gaps,
      zones: [zone({ reason: 'beyond_service_distance' })] } }) });
    expect(items[0].reasonLabel).toBe('超出服务距离');
    const odd = zoneItems({ ...base, report: report({ gaps: { ...report().gaps,
      zones: [zone({ reason: 'something_new' })] } }) });
    expect(odd[0].reasonLabel).toBe('something_new');
  });

  it('reads "no assessment" as unknown area, never as zero', () => {
    const gaps = gapSummary(notAssessed());
    expect(gaps.assessed).toBe(false);
    expect(gaps.gapAreaText).toBeNull();
    expect(gaps.zones).toEqual([]);
    expect(gaps.statusLabel).toBe('未评估');
  });

  it('counts the zones the map will not label', () => {
    const base = snapshot();
    const gaps = gapSummary({ ...base, report: report({ gaps: { ...report().gaps,
      zones: [zone({ id: 'a', labelVisible: true }), zone({ id: 'b', labelVisible: false })] } }) });
    expect(gaps.unlabelled).toBe(1);
    expect(gaps.minLabelAreaM2).toBe(2500);
  });
});

describe('verification', () => {
  it('keeps a returned route with pending strict status as a tolerance estimate', () => {
    const base = snapshot();
    const evidence = { ...base.verification!, facilities: [{ facilityId: 'facility-1',
      routeDistanceM: 438.2, accessDistanceM: 460.1, originOffsetM: 10,
      destinationOffsetM: 11.9, verificationLayer: 'endpoint_tolerance', poiStatus: 'pending',
      poiReason: 'endpoint_mapping_unconfirmed', entranceStatus: 'resolved' }] };
    const current = { ...base, report: null, verification: evidence };
    expect(verificationView(current)).toMatchObject({ routeReturns: 1, strictConfirmed: 0,
      toleranceEstimated: 1, noUsableDecision: 0 });
    expect(verificationFacilityRows(current)[0]).toMatchObject({
      layer: '含端点接入距离估计', routeDistance: '438 米', entrance: 'resolved',
      reason: '路线端点未能严格对应请求的设施位置' });
  });
  it('says so plainly when no verification ran', () => {
    const base = snapshot();
    const view = verificationView({ ...base, verification: null,
      report: report({ verification: { ...report().verification, available: false,
        status: 'not_integrated', checked: 0, provider: null } }) });
    expect(view.available).toBe(false);
    expect(view.statusLabel).toBe('未核验');
    expect(view.summary).toContain('模型推定');
  });

  it('counts what was actually checked', () => {
    const view = verificationView(snapshot());
    expect(view.available).toBe(true);
    expect(view.summary).toContain('已尝试核验 4 处设施');
    expect(view.summary).toContain('严格确认 0 条');
  });
});

describe('evidence notes', () => {
  it('always states the rule, the grid and the limits it was given', () => {
    const notes = evidenceNotes(snapshot());
    const text = notes.map(note => note.text).join('\n');
    expect(text).toContain('服务标准 1000 米');
    expect(text).toContain('网格 50 米');
    expect(text).toContain('目录的完整性未经独立核实');
    expect(text).toContain('目录完整性未经独立核实。');
  });

  it('does not repeat the same sentence twice', () => {
    const notes = evidenceNotes(snapshot({ report: report({
      limitations: ['目录完整性未经独立核实。', '目录完整性未经独立核实。'] }) }));
    const limits = notes.filter(note => note.text === '目录完整性未经独立核实。');
    expect(limits).toHaveLength(1);
  });

  it('gives every line its own key even when several share a code', () => {
    // 一次 OSM 体检会带回五条 ALGORITHM_WARNING。它们说的不是同一件事，所以去重只能按
    // 句子去重；而 key 撞在一起会让 React 丢一条 —— 这一段的意义正是"一条不少"。
    const notes = evidenceNotes(snapshot({ warnings: [
      { code: 'ALGORITHM_WARNING', severity: 'warning', scope: 'isochrone',
        message: 'budget_exhausted' },
      { code: 'ALGORITHM_WARNING', severity: 'warning', scope: 'isochrone',
        message: 'hard_obstacle_water_lines_unresolved' },
      { code: 'FACILITY_WARNING', severity: 'warning', scope: 'facilities',
        message: 'outside_boundary_records' },
    ] }));
    const keys = notes.map(note => note.key);
    expect(new Set(keys).size).toBe(keys.length);
    const text = notes.map(note => note.text).join('\n');
    expect(text).toContain('budget_exhausted');
    expect(text).toContain('hard_obstacle_water_lines_unresolved');
  });

  it('keeps the caveats that apply when a stage did not run', () => {
    const notes = evidenceNotes(notAssessed());
    const text = notes.map(note => note.text).join('\n');
    // 没有热力、没有网格，这两句就不该出现 —— 说了反而是假话。
    expect(text).not.toContain('热力按网格');
    expect(text).not.toContain('网格 ');
    expect(text).toContain('服务标准 1000 米');
  });
});

describe('data sources', () => {
  it('says it cannot name the water data for a revision that predates water evidence', () => {
    const view = dataSourcesView(snapshot());
    expect(view.available).toBe(false);
    expect(view.obstacle).toContain('早于水系证据');
  });

  it('names the OSM version, the review, its bridges, misdrawn areas and unresolved conflicts', () => {
    const view = dataSourcesView(snapshot({ water: water() }));
    expect(view.available).toBe(true);
    expect(view.obstacle).toContain('OpenStreetMap synthetic-osm');
    expect(view.obstacle).toContain('百度底图上的水面只作显示');
    expect(view.reviews).toHaveLength(1);
    expect(view.reviews[0]).toMatchObject({ label: 'synthetic-river@2026-09-01.1', crossings: 1, misdrawn: 1 });
    expect(view.reviews[0].reaches[0]).toContain('按实测宽 18 m 成面');
    expect(view.reviews[0].conflicts[0]).toContain('未裁决');
    expect(view.areas).toContain('数据冲突／未知 660 m²');
  });

  it('prefers the frozen report section over the live snapshot', () => {
    const frozen = { water: { ...water(), reviews: [{ label: 'frozen@1', title: '冻结', crossings: 9,
      basemapMisdrawn: 11, reaches: [], conflicts: [] }] } };
    const view = dataSourcesView(snapshot({ water: water(), report: report({ dataSources: frozen }) }));
    expect(view.reviews[0]).toMatchObject({ label: 'frozen@1', crossings: 9, misdrawn: 11 });
  });

  it('copies water statements into the evidence notes and names the conflict reason', () => {
    const notes = evidenceNotes(snapshot({ water: water() }));
    expect(notes.map(note => note.text)).toContain('水系障碍取自合成 OSM。');
    expect(reasonLabel('water_data_conflict')).toBe('水系数据冲突（未裁决）');
  });
});

