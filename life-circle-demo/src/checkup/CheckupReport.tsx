import { continuationLabel } from './capabilities';
/**
 * v2 体检报告：覆盖率区间、灰区清单与证据说明。
 *
 * 这个组件**不含判断**：每个数字、每条建议、每句限制说明都来自冻结的那一版修订，这里
 * 只负责排版（`report.ts` 负责翻译）。所以它没有分支去"算一下"—— 一旦这里出现
 * `coveredM2 / domainAreaM2`，判据就有了第二处，而第二处迟早会和第一处不一样。
 *
 * 版式是"左目录、右正文"：每一节只把结论摆在明处，解释、明细与机器可读的修订都收进
 * 可展开的目录项（`Fold`，原生 details，收起时仍在 DOM 里）。
 *
 * 三处措辞是被刻意写死的，改动前要先想清楚：
 * - 缺空间支持的类别显示"无法给出覆盖率"，不显示 0%；
 * - 没做过覆盖评估时显示"未评估"，不显示"0 处灰区"；
 * - 核验未接入时显示"模型推定"，不显示"未发现问题"。
 */
import type { CSSProperties, ReactNode, RefObject } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Tag } from 'antd';
import type { CheckupCompletion, CheckupSnapshot } from './contract';
import { Fold } from './Fold';
import { CompletionSummary } from './CompletionSummary';
import {
  area, coverageGroups, coverageItems, dataSourcesView, evidenceNotes, gapSummary, overallView,
  percent, reportSummary, unavailableLabel, overallUnavailableText,
  verificationView, verificationFacilityRows, type CoverageItem, type DataSourcesView, type GapSummary,
} from './report';
import { outdatedText, recomputedText, versionView, type WaterReviewRef } from './water';
import './checkup.css';

/**
 * 区间条：实色是下界 C / A，斜线是未知 U / A（可能覆盖），空白是其余。
 * 只画后端给的两个百分比；不支持的类别画成虚线空条，不画成 0。
 */
function Meter({ lower, upper, color }: { lower: number | null; upper: number | null; color?: string }) {
  if (lower === null || upper === null) return <span className="wb-meter wb-meter-empty" aria-hidden="true" />;
  return <span className="wb-meter" aria-hidden="true"
    style={color ? { '--meter': color } as CSSProperties : undefined}>
    <i className="wb-meter-known" style={{ width: `${lower}%` }} />
    <i className="wb-meter-unknown" style={{ left: `${lower}%`, width: `${Math.max(0, upper - lower)}%` }} />
  </span>;
}

/** 目录与正文共用的章节表：锚点只在报告内部滚动，不写进地址栏（地址栏归算法路由用）。 */
const SECTIONS = [
  { id: 'rp-overall', title: '总体覆盖' },
  { id: 'rp-categories', title: '分类覆盖' },
  { id: 'rp-gaps', title: '服务盲区' },
  { id: 'rp-verification', title: '现实核验' },
  { id: 'rp-notes', title: '证据说明' },
  { id: 'rp-sources', title: '数据来源' },
] as const;
type SectionId = typeof SECTIONS[number]['id'];

function Section({ id, children }: { id: SectionId; children: ReactNode }) {
  const title = SECTIONS.find(section => section.id === id)?.title;
  return <section id={id} className="rp-sec"><h2>{title}</h2>{children}</section>;
}

/**
 * 左侧目录：点一项就在抽屉里滚到那一节，滚动时高亮正在读的那一节。
 *
 * 不用 antd Anchor：它点击时会改写 location.hash，而 hash 是 `#/checkup/*` 的算法路由。
 */
function Toc({ root }: { root: RefObject<HTMLElement | null> }) {
  const [active, setActive] = useState<SectionId>(SECTIONS[0].id);
  useEffect(() => {
    const element = root.current;
    // 抽屉正文才是滚动容器。
    const scroller = element?.closest('.ant-drawer-body');
    if (!element || !scroller) return;
    // 标题已经滚过容器上沿以下 96 像素的最后一节就是正在读的那一节；滚到底时算最后一节，
    // 否则篇幅短的末节永远亮不起来。
    const update = () => {
      const line = scroller.getBoundingClientRect().top + 96;
      let current: SectionId = SECTIONS[0].id;
      for (const section of SECTIONS) {
        const target = element.querySelector(`#${section.id}`);
        if (target && target.getBoundingClientRect().top <= line) current = section.id;
      }
      if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) {
        current = SECTIONS[SECTIONS.length - 1].id;
      }
      setActive(current);
    };
    update();
    scroller.addEventListener('scroll', update, { passive: true });
    return () => scroller.removeEventListener('scroll', update);
  }, [root]);
  function go(id: SectionId) {
    root.current?.querySelector(`#${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    setActive(id);
  }
  return <nav className="rp-toc" aria-label="报告目录">
    <p className="rp-toc-title">目录</p>
    <ul>{SECTIONS.map(section => <li key={section.id}>
      <button type="button" aria-current={active === section.id ? 'true' : undefined}
        onClick={() => go(section.id)}>{section.title}</button>
    </li>)}</ul>
  </nav>;
}

/** 分类覆盖一行：区间与区间条在上，三类面积在下；不支持的类别只说"无法给出"，不写 0%。 */
function CoverageRow({ item, count }: { item: CoverageItem; count?: number }) {
  const name = <span className="rp-cat"><i style={{ background: item.color }} />{item.label}
    {item.supported && item.evidenceGrade
      && <small>{item.evidenceGrade === 'verified' ? '已核验' : '模型推定'}</small>}</span>;
  if (!item.supported) return <li className="rp-cat-row" data-testid={`coverage-${item.category}`}>
    <div className="rp-cat-head">{name}</div>
    <p>已检索设施 {count ?? '未记录'} 处 · 检索{item.queryComplete ? '已完成' : '未完成'}</p>
    <p className="rp-unsupported">无法给出覆盖率：{unavailableLabel(item.unavailableReason)}。未知不等于"没有设施"。</p>
  </li>;
  return <li className="rp-cat-row" data-testid={`coverage-${item.category}`}>
    <div className="rp-cat-head">{name}
      <span className="rp-num rp-cat-range">{percent(item.lowerPct)} ～ {percent(item.upperPct)}</span></div>
    <p>已检索设施 {count ?? '未记录'} 处 · 检索{item.queryComplete ? '已完成' : '未完成'}
      {item.assessablePct === 0 && <strong> · 全部未知，尚无有效覆盖判定</strong>}</p>
    <Meter lower={item.lowerPct} upper={item.upperPct} color={item.color} />
    <p className="rp-areas">
      <span><em>已覆盖</em>{area(item.coveredM2)}</span>
      <span><em>缺口</em>{area(item.gapM2)}{item.gapM2 === 0
        && <small>（未知 {percent(item.unknownPct)}；检索{item.queryComplete === true
          ? '完整' : item.queryComplete === false ? '未完成' : '完整性未记录'}）</small>}</span>
      <span><em>未知</em>{area(item.unknownM2)}</span>
      <span><em>可评估</em>{percent(item.assessablePct)}</span>
    </p>
  </li>;
}

function ZoneList({ gaps }: { gaps: GapSummary }) {
  if (!gaps.assessed) return <Alert type="warning" showIcon
    title="本次未进行服务覆盖评估，因此没有灰区清单"
    description="灰区面积与条数都需要评估域和步行路网；缺少其中之一时就只能写“未评估”，不能写成 0。" />;
  return <>
    <p className="rp-lead">灰区合计 <b className="rp-num">{gaps.gapAreaText}</b>
      <span className="rp-sep">·</span>至少两类同时缺失 <b className="rp-num">{gaps.compositeAreaText}</b></p>
    {gaps.byCategory.length > 0 && <p className="rp-chips">{gaps.byCategory.map(row =>
      <span key={row.label}>{row.label} {row.areaText}</span>)}</p>}
    {gaps.zones.length === 0 ? <p className="checkup-muted">本次评估没有产生灰区。</p>
      : <ol className="checkup-zones">{gaps.zones.map(zone => <li key={zone.id}
        data-testid={`zone-${zone.id}`} data-status={zone.queryStatus}>
        <header><b>{zone.title}</b><Tag>{zone.kindLabel}</Tag>
          {zone.queryStatus !== 'complete' && <Tag color="orange">设施检索未完成</Tag>}
          <span className="checkup-zone-area">{zone.areaText}</span></header>
        <p><strong>发现的问题：</strong>{zone.reasonLabel ?? '模型识别出服务覆盖缺口'}
          （{zone.evidenceGrade === 'verified' ? '已核验' : '模型推定'}）。</p>
        <p><strong>涉及类别：</strong>{zone.categoryLabels.join('、')}</p>
        <p><strong>建议下一步：</strong>{zone.suggestion || '结合设施目录与实际步行路线进一步核实。'}</p>
        <Fold title="详情" className="rp-fold-inline">
          <p className="checkup-muted">涉及类别：{zone.categoryLabels.join('、')}；几何分量
            {zone.parts} 片；证据等级 {zone.evidenceGrade === 'verified' ? '已核验' : '模型推定'}。
            {zone.reasonLabel && `原因：${zone.reasonLabel}。`}
            {zone.nearestFacility && `最近设施：${zone.nearestFacility}。`}
            {!zone.labelled && '面积小于标注阈值，地图上不标注。'}</p>
        </Fold>
      </li>)}</ol>}
    {(gaps.unlabelled > 0 || gaps.notes.length > 0) && <Fold title="灰区说明" count={gaps.notes.length
      + (gaps.unlabelled > 0 ? 1 : 0)}>
      {gaps.unlabelled > 0 && <p className="checkup-muted">另有 {gaps.unlabelled} 处面积小于标注阈值{gaps.minLabelAreaM2
        ? `（${area(gaps.minLabelAreaM2)}）` : ''}，地图上不标注，但仍在灰区面积内。</p>}
      {gaps.notes.map((note, index) => <p key={index} className="checkup-muted">{note}</p>)}
    </Fold>}
  </>;
}

function DataSources({ view }: { view: DataSourcesView }) {
  return <>
    <p className="checkup-muted">{view.obstacle}</p>
    {view.available && view.reviews.length === 0 && <p className="checkup-muted">评估域不在任何水系复核范围内：
      水系按 OSM 原样计算，未与影像或第二家地图核对。</p>}
    {view.reviews.map(review => <article key={review.label} className="checkup-card"
      data-testid={`water-review-${review.label}`}>
      <header><strong>{review.title}</strong><Tag>{review.label}</Tag></header>
      <Fold title="复核明细" className="rp-fold-inline">
        <dl className="checkup-facts">
          {review.reviewedAt && <><dt>复核日期</dt><dd>{review.reviewedAt}</dd></>}
          {review.scope && <><dt>适用范围</dt><dd>{review.scope}</dd></>}
          {review.sources.length > 0 && <><dt>依据</dt><dd>{review.sources.join('；')}</dd></>}
          {review.method && <><dt>方法</dt><dd>{review.method}</dd></>}
          {review.reaches.length > 0 && <><dt>已核实河道</dt><dd>{review.reaches.join('；')}</dd></>}
          <dt>已核实桥梁</dt><dd>{review.crossings} 座</dd>
          <dt>底图误绘</dt><dd>{review.misdrawn} 处（已核实为陆地，按陆地计算）</dd>
          <dt>数据冲突／未知</dt><dd>{review.conflicts.length === 0 ? '无' : review.conflicts.join('；')}</dd>
        </dl>
        {review.limitations.length > 0 && <ul className="checkup-notes">{review.limitations.map(item =>
          <li key={item} className="checkup-note-warning">{item}</li>)}</ul>}
      </Fold>
    </article>)}
    {view.areas && <p className="checkup-muted">{view.areas}</p>}
    {view.rejected.map(item => <p key={item} className="checkup-muted">未采用的复核：{item}</p>)}
  </>;
}

const NO_REVIEWS: readonly WaterReviewRef[] = [];

export function CheckupReport({ snapshot, stale, waterReviews = NO_REVIEWS, continuation,
  busy = false, continuationPoiLimit = null, onContinue, onCancel }: {
  snapshot: CheckupSnapshot;
  stale?: boolean;
  /** 当前部署采用的水系复核（能力表）；用来认出早于复核的旧版本。 */
  waterReviews?: readonly WaterReviewRef[];
  continuation?: CheckupCompletion | null;
  busy?: boolean;
  continuationPoiLimit?: number | null;
  onContinue?: () => void;
  onCancel?: () => void;
}) {
  // 按修订记忆：`snapshot` 的身份只在取到新的一版时变化，所以这一组派生值在一次体检
  // 里是稳定的，图表也就不会因为父组件重渲染而重建。
  const items = useMemo(() => coverageItems(snapshot), [snapshot]);
  const groups = useMemo(() => coverageGroups(items), [items]);
  const summary = useMemo(() => reportSummary(snapshot), [snapshot]);
  const overall = useMemo(() => overallView(snapshot), [snapshot]);
  const gaps = useMemo(() => gapSummary(snapshot), [snapshot]);
  const verification = useMemo(() => verificationView(snapshot), [snapshot]);
  const verificationRows = useMemo(() => verificationFacilityRows(snapshot), [snapshot]);
  const notes = useMemo(() => evidenceNotes(snapshot), [snapshot]);
  const sources = useMemo(() => dataSourcesView(snapshot), [snapshot]);
  const version = useMemo(() => versionView(snapshot, waterReviews), [snapshot, waterReviews]);
  const domainAreaM2 = snapshot.accessibility?.domainAreaM2 ?? snapshot.report?.domainAreaM2 ?? null;
  const generated = new Date(snapshot.generatedAt * 1000).toLocaleString('zh-CN', { hour12: false });
  const root = useRef<HTMLElement>(null);

  return <article className="checkup-report" data-testid="checkup-report" ref={root}
    data-task-id={snapshot.taskId} data-revision={snapshot.revision}>
    <Toc root={root} />
    <div className="rp-main">
      <header className="rp-head">
        <p className="rp-kicker">{snapshot.engine.label} · 第 {snapshot.revision} 版 · {generated}</p>
        <h1>15 分钟生活圈体检报告</h1>
        <CompletionSummary value={snapshot.report?.completion ?? snapshot.completion ?? (!busy ? continuation : null)} />
        {busy && <Alert type="info" title={`正在补全；正文保留第 ${snapshot.revision} 版报告`} />}
        {onContinue && continuation?.canContinue && !busy && <Button type="primary" disabled={stale}
          onClick={onContinue} data-testid="report-continue">
          {continuationLabel(continuation.restartRetrieval, continuationPoiLimit)}
        </Button>}
        {busy && onCancel && <Button onClick={onCancel}>停止本轮补全</Button>}
        {stale && <Alert type="warning" showIcon title="条件已修改，本报告仍属于原中心点的那一次体检。" />}
        {version.outdatedBy.length > 0 && <Alert type="warning" showIcon data-testid="report-outdated"
          title="旧版本：水系数据已修订" description={outdatedText(version)} />}
        {snapshot.businessStatus !== 'complete' && <p className="rp-status"
          data-tone={snapshot.businessStatus === 'insufficient' ? 'warn' : 'info'}>
          {snapshot.businessStatus === 'insufficient' ? '证据不足：结论只覆盖已评估的部分'
            : '部分结果：有阶段未能完成，未知部分不计入已知覆盖，仍可能影响覆盖区间上界。'}</p>}
      </header>

      <section className="rp-summary" aria-label="结论摘要" data-testid="report-summary">
        <h2>先看结论</h2>
        <dl>
          <dt>覆盖情况</dt><dd>{summary.coverage}</dd>
          <dt>主要缺口</dt><dd>{summary.gap}</dd>
          <dt>现实核验</dt><dd>{summary.verification}</dd>
        </dl>
        <p className="checkup-muted">覆盖区间的下界表示已知覆盖，上界包含未知面积；未知区域仍需补充证据。</p>
        {summary.limitations.length > 0 && <div className="rp-summary-limits">
          <h3>阅读结论时请留意</h3>
          <ul>{summary.limitations.map(note => <li key={note.key}>{note.text}</li>)}</ul>
        </div>}
      </section>

      <Section id="rp-overall">
        {overall.available ? <div className="rp-overall">
          <p className="checkup-interval checkup-overall">
            <b>{percent(overall.lowerPct)}</b><span>～</span><b>{percent(overall.upperPct)}</b></p>
          <Meter lower={overall.lowerPct} upper={overall.upperPct} />
          <p className="wb-meter-key"><i className="k-known" />已知覆盖（下界）<i className="k-unknown" />未知面积
            <span>可评估 {percent(overall.assessablePct)} · 未知 {percent(overall.unknownPct)}</span></p>
          <Fold title="怎么读这个区间">
            <p className="checkup-muted">下界是"已知覆盖"，上界是"已知覆盖 + 未知面积"，两者之间
              就是本次路面证据说不到的地方。</p>
          </Fold>
        </div> : <Alert type="info" showIcon title="本次不给总体覆盖率"
          description={overallUnavailableText(snapshot)} />}
      </Section>

      <Section id="rp-categories">
        {groups.assessed.length > 0 && <>
          <p className="checkup-muted">按缺口面积从大到小排列；缺少面积记录的类别列在后面。</p>
          <ul className="rp-cats">{groups.assessed.map(item => <CoverageRow key={item.category} item={item}
            count={snapshot.facilities?.countsByCategory?.[item.category]} />)}</ul>
        </>}
        {groups.unavailable.length > 0 && <div data-testid="coverage-unavailable">
          <h3>暂无法评估</h3>
          <ul className="rp-cats">{groups.unavailable.map(item => <CoverageRow key={item.category} item={item}
            count={snapshot.facilities?.countsByCategory?.[item.category]} />)}</ul>
        </div>}
        {items.length === 0 && <p className="checkup-muted">尚无分类覆盖结果，暂无法评估。</p>}
        <p className="checkup-muted">最低覆盖率 = C / A，最高覆盖率 = (C + U) / A；C 已覆盖、U 未知，
          A 为评估域面积 {area(domainAreaM2)}。</p>
        {items.some(item => item.degenerate) && <Fold title="区间退化说明">
          {items.filter(item => item.degenerate).map(item => <p key={item.category} className="checkup-muted">
            {item.label}：区间退化为一个点。本类别的每一格都已判定，没有未知面积 —— 这不是精度更高，
            只是恰好没有留下没查到的部分。</p>)}
        </Fold>}
      </Section>

      <Section id="rp-gaps">
        <ZoneList gaps={gaps} />
      </Section>

      <Section id="rp-verification">
        <p className="rp-lead" data-testid="verification-summary">{verification.summary}
          {verification.provider && `（${verification.provider}）`}</p>
        {verificationRows.length > 0 && <Fold title="逐设施路线证据" count={`${verificationRows.length} 条`}>
          <ul className="checkup-notes">{verificationRows.map((row, index) => <li key={`${row.id}:${index}`}>
            <strong>{row.id}</strong>：{row.layer}；实际路线 {row.routeDistance}；
            接入距离 {row.accessDistance}；起点偏移 {row.originOffset}，终点偏移 {row.destinationOffset}；
            {row.entrance}{row.reason && `；未确认原因：${row.reason}`}
          </li>)}</ul>
        </Fold>}
        {(verification.reason || !verification.available) && <Fold title="详情">
          {verification.reason && <p className="checkup-muted">{verification.reason}</p>}
          {!verification.available && <p className="checkup-muted">没有核验不是"核验过、没问题"：
            设施的可达性判断来自路网模型，实地情况仍可能不同。</p>}
        </Fold>}
      </Section>

      <Section id="rp-notes">
        <Fold title="全部说明" count={`${notes.length} 条`}>
          <ul className="checkup-notes">{notes.map(note => <li key={note.key}
            className={note.level === 'warning' ? 'checkup-note-warning' : undefined}>{note.text}</li>)}</ul>
        </Fold>
        {snapshot.warnings.length > 0 && <Fold title="算法质量标记" count={`${snapshot.warnings.length} 条`}>
          {/* 这些标记往往共用同一个 code（一次 OSM 体检有五条 ALGORITHM_WARNING），只用 code
              当 key 会让 React 认不出孩子，而这一块的意义就是"一条不少"，所以按位置认。 */}
          <ul className="checkup-notes">{snapshot.warnings.map((warning, index) => <li key={index}>
            [{warning.severity}] {warning.code}：{warning.message}（{warning.scope}）</li>)}</ul>
        </Fold>}
      </Section>

      <section id="rp-sources" className="rp-sec" data-testid="report-data-sources">
        <h2>数据来源</h2>
        <DataSources view={sources} />
        <Fold title="任务信息">
          <dl className="checkup-facts">
            <dt>任务 / 修订</dt><dd>{snapshot.taskId} · 第 {snapshot.revision} 版</dd>
            <dt>中心点</dt><dd>{snapshot.center.lng.toFixed(6)}, {snapshot.center.lat.toFixed(6)}</dd>
            <dt>生成时间</dt><dd>{generated}</dd>
            <dt>算法</dt><dd>{snapshot.engine.label}（{snapshot.engine.engineId}）</dd>
            <dt>判定依据</dt><dd>步行距离 {snapshot.rules.threshold_m} 米 ·
              {snapshot.rules.metric === 'walking_route' ? '步行路线' : snapshot.rules.metric} ·
              坐标系 BD09LL</dd>
            <dt>结果指纹</dt><dd>{snapshot.trace.resultHash}</dd>
            <dt>水系数据</dt><dd data-testid="report-water-version">{version.applied === null ? '早于水系复核（无记录）'
              : version.applied.length === 0 ? '未采用复核（OSM 原样）' : version.applied.join('、')}</dd>
            {version.recomputed && <><dt>版本来源</dt>
              <dd data-testid="report-recomputed">{recomputedText(version.recomputed)}</dd></>}
          </dl>
        </Fold>
        <Fold title="机器可读修订"><pre>{JSON.stringify(snapshot, null, 2)}</pre></Fold>
      </section>
    </div>
  </article>;
}
