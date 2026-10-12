/**
 * v2 体检工作台。
 *
 * 与旧分析页的分工：**这里只负责"提交什么、显示什么、把哪一层画上去"**。判据在
 * `capabilities.ts`（能选什么）、`report.ts`（数字怎么读）、`layers.ts`（画成什么），
 * 三个都是纯函数，所以这个组件里剩下的东西都是排版。
 *
 * 三处刻意的选择：
 *
 * - **引擎与预算从能力表里来**，不写死档位。引擎换掉时预算跟着换：档位是引擎自己的，
 *   沿用上一个引擎的档会撞上"不支持的预算一律 422"（§4.1）。
 * - **余额照抄后端给的说法**（§9 的"本应用预算余额"），不加也不改。它只包含本应用自己的
 *   额度，浏览器 SDK、其他应用和旧接口的流量都不在里面 —— 换个说法就会被认为是账号总余额。
 * - **图层按修订取、按层失败按层说**。一层没就绪（409 带名字）不该让整页失败，也不该
 *   被画成"这一层是空的"。
 */
import { useEffect, useMemo, useRef, useState, type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';
import { Alert, Button, Checkbox, Descriptions, Drawer, InputNumber, Select, Space } from 'antd';
import type { Center } from '../types';
import { LocationControls } from '../analysis/LocationControls';
import { createCheckupService, CheckupError } from './client';
import { budgetFor, capabilityView, hybridTimeEstimate, continuationLabel, type CapabilityView } from './capabilities';
import { isCheckupBusy, isTerminal, STAGE_LABELS } from './types';
import type { CheckupState } from './types';
import { checkupSession, useCheckupState } from './sessions';
import { LAYER_IDS, type LayerId, type Stage } from './validate';
import { DENSITY_ALL, drawableLayer, LAYER_STYLES, serviceSamples, type LayerDrawable } from './layers';
import { CheckupMap, DEFAULT_CHECKUP_LAYERS, layerSwatch, type CheckupLayerToggles, type HeatLayer } from './CheckupMap';
import { CheckupReport } from './CheckupReport';
import { Fold } from './Fold';
import { CATEGORY_ORDER, categoryLabel, coverageItems, percent, reportDirectory, reasonLabel,
  verificationLayerLabel, overallUnavailableText } from './report';
import { nearestFacilities } from './nearest';
import { missingCoreMajors, estimatedFirstRoundPages, splitScope } from './categories';
import { extensionLines } from './extensions';
import { isRetryTerminal, retryActionLabel, retryLines } from './retry';
import { expiryLine, expiryNotice, retainedSummaryLines } from './retained';
import type { MajorCategory } from './contract';
import { nearestEmptyNote } from './nearest';
import { queryCoverageLine, queryCoverageRetryLine } from './queryCoverage';
import { WeatherCard } from './WeatherCard';
import { SERVICE_COMPOSITE } from '../map/layers/serviceField';
import { outdatedText, recomputedText, versionView, waterView } from './water';
import { CompletionSummary } from './CompletionSummary';
import { duration, liveView, TICKING_PHASES, type LiveKind } from './live';
import './checkup.css';

/** 地图上的六层；报告是文档，不占图层开关，从这里打开。 */
const MAP_LAYERS = LAYER_IDS.filter(id => id !== 'report') as LayerId[];

/** 两种热力各自要先取到的图层：打开热力时，即使这些图层本身没勾选也要取。 */
const HEAT_DEPENDENCIES: Record<HeatLayer, LayerId[]> = {
  service: ['heatmap', 'isochrone'],
  density: ['facilities', 'isochrone'],
};

const SERVICE_MODES = [
  { value: SERVICE_COMPOSITE, label: '综合（全部类别均已知处）' },
  ...CATEGORY_ORDER.map(category => ({ value: category, label: categoryLabel(category) })),
];

const DENSITY_CATEGORIES = [
  { value: DENSITY_ALL, label: '全部设施' },
  ...CATEGORY_ORDER.map(category => ({ value: category, label: categoryLabel(category) })),
];

/** 存下来的选项不在当前列表里（旧版本存的、被手改过的）就回到默认，不把未知值交给地图。 */
const known = (options: { value: string }[], value: string | undefined, fallback: string) =>
  value !== undefined && options.some(option => option.value === value) ? value : fallback;

const BUSINESS_LABELS: Record<string, string> = {
  complete: '证据完整', partial: '部分证据', insufficient: '证据不足',
};
const ROUTE_VERDICT: Record<string, string> = {
  verified_reachable: '已核验可达', verified_unreachable: '已核验不可达', pending: '尚未核验',
};
const EVIDENCE_LABELS: Record<string, string> = { verified: '真实路线', model: '模型推定' };

function StageProgress({ stage }: { stage: Stage | null | undefined }) {
  const order = Object.keys(STAGE_LABELS) as Stage[];
  const current = stage === null || stage === undefined ? -1 : order.indexOf(stage);
  return <ol className="checkup-stages" data-testid="checkup-stages">
    {order.map((name, index) => <li key={name}
      data-reached={current >= index ? 'yes' : 'no'}
      aria-current={current === index ? 'step' : undefined}>
      <span className="checkup-stage-index">{index + 1}</span>{STAGE_LABELS[name]}</li>)}
  </ol>;
}

/**
 * 区间条：实色是下界（已知覆盖），斜线是未知面积（可能覆盖），空白是其余。
 * 只把后端给的两个百分比画出来，不另算任何数。
 */
function RangeMeter({ lower, upper, color }: { lower: number | null; upper: number | null; color?: string }) {
  if (lower === null || upper === null) return <span className="wb-meter wb-meter-empty" aria-hidden="true" />;
  return <span className="wb-meter" aria-hidden="true" style={color ? { '--meter': color } as CSSProperties : undefined}>
    <i className="wb-meter-known" style={{ width: `${lower}%` }} />
    <i className="wb-meter-unknown" style={{ left: `${lower}%`, width: `${Math.max(0, upper - lower)}%` }} />
  </span>;
}

const fixed = (value: number | null | undefined) => value === null || value === undefined ? '—' : value.toFixed(1);

const PHASE_TONE: Partial<Record<CheckupState['phase'], string>> = {
  completed: 'done', cancelled: 'muted', error: 'error',
};

/** 一条设施路线的读法：判定与距离同进同出，没有距离就不说"在不在标准内"。 */
function RouteDetail({ route, error, facilityName }: { route: CheckupState['route']; error?: string;
  facilityName?: string | null }) {
  if (error) return <Alert type="warning" title={error} showIcon />;
  if (!route) return null;
  return <>
    <Descriptions className="wb-facts" size="small" column={1} items={[
      { key: 'facility', label: '设施', children: facilityName ?? route.facilityId },
      { key: 'category', label: '类别', children: `${categoryLabel(route.category)}（${route.majorCategory
        ? categoryLabel(route.majorCategory) : '未分类'}）` },
      { key: 'status', label: '检索状态', children: ROUTE_VERDICT[route.poiStatus] ?? route.poiStatus },
      { key: 'straight', label: '直线距离', children: route.straightLineM === null ? '无法确定' : `${route.straightLineM.toFixed(0)} 米` },
      // 判定跟着距离一起给；只有判定没有距离的响应在校验层就被拒了，这里不必兜底。
      { key: 'distance', label: '步行距离', children: route.routeDistanceM === null ? '无法确定' : `${route.routeDistanceM.toFixed(0)} 米` },
      { key: 'layer', label: '证据层级', children: verificationLayerLabel(route.verificationLayer) },
      { key: 'access', label: '接入距离估计', children: route.accessDistanceM === null
        ? '无法确定' : `${route.accessDistanceM.toFixed(0)} 米` },
      { key: 'offsets', label: '两端偏移', children: `${fixed(route.originOffsetM)} 米 / ${fixed(route.destinationOffsetM)} 米` },
      { key: 'poi', label: '严格端点状态', children: ROUTE_VERDICT[route.poiStatus] ?? route.poiStatus },
      { key: 'entrance', label: '入口状态', children: route.entranceStatus === 'unresolved'
        ? '入口未解决' : route.entranceStatus ?? '本次详情未记录入口状态' },
      { key: 'verdict', label: '服务标准', children: route.withinRule === null ? '无法判定'
        : route.withinRule ? '在 1000 米步行范围内' : '超出 1000 米步行范围' },
      { key: 'grade', label: '证据等级', children: EVIDENCE_LABELS[route.evidenceGrade] ?? route.evidenceGrade },
      { key: 'provider', label: '来源', children: `${route.provider}${route.network ? '' : '（本地缓存）'}` },
    ]} />
    {(route.poiReason || route.reason) && <Alert type="info"
      title={reasonLabel(route.poiReason) ?? reasonLabel(route.reason) ?? '未确认'} />}
    {route.notes.length > 0 && <ul className="checkup-notes">{route.notes.map(note =>
      <li key={note}>{note}</li>)}</ul>}
  </>;
}

/** 实时读法的提示框：问不到后端与疑似停滞要人留意，其余只是说明。 */
const LIVE_ALERT: Record<LiveKind, 'info' | 'warning'> = {
  submitting: 'info', restoring: 'info', queued: 'info', working: 'info', cancelling: 'info',
  lost: 'warning', stalled: 'warning',
};

/** 只有时钟在走：任务未决时每秒取一次本机时刻重算读数，不做任何动画。 */
function useClock(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

const DEFAULT_CENTER: Center = { lng: 116.404, lat: 39.915 };
/** 这些阶段出现过，完成时才自动打开报告；从刷新恢复出来的"已完成"按上次的开合状态来。 */
const LIVE_PHASES = new Set(['submitting', 'queued', 'running']);

const TAB_KEYS = ['location', 'engine', 'layers'] as const;
type TabKey = typeof TAB_KEYS[number];
const TAB_LABELS: Record<TabKey, string> = {
  location: '我的位置', engine: '采样与引擎', layers: '图层备注',
};
const isTabKey = (value: string | undefined): value is TabKey =>
  value !== undefined && (TAB_KEYS as readonly string[]).includes(value);
let lastTab: TabKey = 'location';

/** 两套类别集合是否是同一组：顺序不同不算变化，省掉一次多余的请求体差异。 */
function sameScope(left: readonly MajorCategory[], right: readonly MajorCategory[]): boolean {
  return left.length === right.length && left.every(key => right.includes(key));
}

const EXTENSION_STATUS_LABELS: Record<string, string> = {
  queued: '排队中', running: '检索中', completed: '已完成', partial: '部分完成',
  failed: '未完成', cancelled: '已取消',
};
const EXTENSION_DONE = new Set(['completed', 'partial', 'failed', 'cancelled']);

type PanelId = 'side' | 'results';
type PanelLayout = { x: number; y: number; width: number; height: number };
type WorkspaceSize = { width: number; height: number };
type PanelInteraction = {
  panel: PanelId;
  mode: 'move' | 'resize';
  pointerId: number;
  startX: number;
  startY: number;
  initial: PanelLayout;
};

const LEGEND_GAP = 12;
const LEGEND_BOTTOM_MARGIN = 28;
const LEGEND_HEADER_ALLOWANCE = 48;

const PANEL_SIZE_LIMITS: Record<PanelId, { minWidth: number; maxWidth: number }> = {
  side: { minWidth: 300, maxWidth: 480 }, results: { minWidth: 320, maxWidth: 520 },
};

function boundedLayout(layout: PanelLayout, panel: PanelId, workspace: WorkspaceSize,
  legendHeight = 0): PanelLayout {
  const limits = PANEL_SIZE_LIMITS[panel];
  const maxWidth = Math.min(limits.maxWidth, Math.max(limits.minWidth, workspace.width - 32));
  // The left panel and its legend move as one column. Leave 12px between
  // them and 28px below the legend for map attribution.
  const bottom = panel === 'side' && legendHeight > 0 ? legendHeight + LEGEND_GAP + LEGEND_BOTTOM_MARGIN : 16;
  const maxHeight = Math.min(760, Math.max(280, workspace.height - 16 - bottom));
  const width = Math.min(maxWidth, Math.max(limits.minWidth, layout.width));
  const height = Math.min(maxHeight, Math.max(280, layout.height));
  return {
    x: Math.max(16, Math.min(layout.x, workspace.width - width - 16)),
    y: Math.max(16, Math.min(layout.y, workspace.height - height - bottom)),
    width,
    height,
  };
}

function initialPanelLayouts(): Record<PanelId, PanelLayout> {
  const workspaceHeight = Math.max(320, window.innerHeight - 52);
  const sideHeight = Math.min(620, Math.max(280, workspaceHeight - 32));
  const resultsHeight = Math.min(660, Math.max(280, workspaceHeight - 32));
  return {
    side: { x: 16, y: 16, width: 344, height: sideHeight },
    results: { x: Math.max(16, window.innerWidth - 376), y: 16, width: 360, height: resultsHeight },
  };
}

const PHASE_LABELS: Partial<Record<CheckupState['phase'], string>> = {
  submitting: '提交中', restoring: '恢复中', queued: '排队中', running: '运行中',
  fetching: '正在取结果', cancelling: '正在取消', completed: '已完成', cancelled: '已取消', error: '需要处理',
};

/**
 * 体检工作台。`engine` 由外层的算法切换决定：每个引擎有自己的任务登记（`sessions.ts`），
 * 这个组件只订阅它 —— 卸载时退订，任务照跑；只有"取消任务"按钮会让服务端停下。
 */
export default function CheckupApp({ engine, algorithmSwitch }: { engine: string; algorithmSwitch?: ReactNode }) {
  const service = useMemo(() => createCheckupService(), []);
  const session = useMemo(() => checkupSession(engine), [engine]);
  const controller = session.controller;
  const state = useCheckupState(session);
  const saved = useMemo(() => session.prefs(), [session]);
  const workspaceRef = useRef<HTMLElement>(null);
  const [workspaceSize, setWorkspaceSize] = useState<WorkspaceSize>(() => ({
    width: window.innerWidth, height: Math.max(320, window.innerHeight - 52),
  }));
  const [panelLayouts, setPanelLayouts] = useState<Record<PanelId, PanelLayout>>(initialPanelLayouts);
  const [legendHeight, setLegendHeight] = useState(0);
  const [frontPanel, setFrontPanel] = useState<PanelId>('side');
  const panelInteraction = useRef<PanelInteraction | null>(null);
  const wasFloating = useRef(window.innerWidth > 1080 && window.innerHeight >= 560);
  const [tab, setTab] = useState<TabKey>(isTabKey(saved.tab) ? saved.tab : lastTab);
  const chooseTab = (key: TabKey) => { lastTab = key; setTab(key); };
  const initial = saved.draft ?? state.input?.center ?? DEFAULT_CENTER;
  const [center, setCenter] = useState<Center>(initial);
  const [lng, setLng] = useState<number | null>(initial.lng);
  const [lat, setLat] = useState<number | null>(initial.lat);
  const [budget, setBudget] = useState<number | null>(saved.budget ?? null);
  const [view, setView] = useState<CapabilityView | null>(null);
  const [capabilityError, setCapabilityError] = useState<string | null>(null);
  const [toggles, setToggles] = useState<CheckupLayerToggles>({ ...DEFAULT_CHECKUP_LAYERS, ...saved.toggles });
  const [serviceMode, setServiceMode] = useState<string>(known(SERVICE_MODES, saved.serviceMode, SERVICE_COMPOSITE));
  const [densityCategory, setDensityCategory] = useState<string>(
    known(DENSITY_CATEGORIES, saved.densityCategory, DENSITY_ALL));
  const [layerErrors, setLayerErrors] = useState<Partial<Record<LayerId, string>>>({});
  /** 哪一层在哪一版上失败过：同一版不反复重试，换版再试。 */
  const [failed, setFailed] = useState<Partial<Record<LayerId, number>>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [reportOpen, setReportOpen] = useState(saved.reportOpen ?? false);

  useEffect(() => {
    const workspace = workspaceRef.current;
    if (!workspace) return;
    const measure = () => {
      const bounds = workspace.getBoundingClientRect();
      const nextSize = { width: bounds.width, height: bounds.height };
      setWorkspaceSize(nextSize);
      const floating = nextSize.width > 1080 && window.innerHeight >= 560;
      const enteringFloating = floating && !wasFloating.current;
      wasFloating.current = floating;
      setPanelLayouts(current => {
        const side = boundedLayout(current.side, 'side', nextSize);
        const results = boundedLayout(current.results, 'results', nextSize);
        return {
          side,
          results: { ...results, x: enteringFloating ? nextSize.width - results.width - 16 : results.x },
        };
      });
    };
    const observer = new ResizeObserver(measure);
    observer.observe(workspace);
    window.addEventListener('resize', measure);
    measure();
    return () => { observer.disconnect(); window.removeEventListener('resize', measure); };
  }, []);

  // 界面偏好随改随存：刷新或切回来时，图层开关、热力模式、报告开合都按离开时的样子。
  useEffect(() => {
    session.setPrefs({ draft: center, ...(budget === null ? {} : { budget }),
      toggles: toggles as Record<string, boolean>, serviceMode, densityCategory, reportOpen, tab });
  }, [session, center, budget, toggles, serviceMode, densityCategory, reportOpen, tab]);

  useEffect(() => {
    const abort = new AbortController();
    let retryTimer: number | undefined;
    const loadCapabilities = () => service.capabilities(abort.signal).then(value => {
      const next = capabilityView(value);
      setView(next);
      // 档位是引擎自己的：存下来的档位不在这个引擎的档位表里，就换成它自己的默认档。
      const budgets = next.engines.find(item => item.engineId === engine)?.budgets ?? [];
      setBudget(current => current !== null && budgets.includes(current) ? current : budgetFor(next, engine));
      if (engine === 'osm_hybrid' && (next.graphState === 'unloaded' || next.graphState === 'loading')) {
        retryTimer = window.setTimeout(loadCapabilities, 1000);
      }
    }).catch((error: unknown) => {
      // 取消不是故障：组件已经卸载或重新挂载，这一轮的结果不该再上屏。
      if (abort.signal.aborted) return;
      setCapabilityError(error instanceof CheckupError ? error.message
        : '未能读取体检服务能力表，请检查服务地址后刷新页面');
    });
    void loadCapabilities();
    return () => {
      abort.abort();
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [service, engine]);

  const task = state.task;
  const snapshot = state.snapshot;
  const directory = snapshot?.report ? reportDirectory(snapshot) : view?.categoryDirectory ?? [];
  const serviceModes = directory.length ? [SERVICE_MODES[0],
    ...directory.map(item => ({ value: item.id, label: item.label }))] : SERVICE_MODES;
  const densityCategories = directory.length ? [DENSITY_CATEGORIES[0],
    ...directory.map(item => ({ value: item.id, label: item.label }))] : DENSITY_CATEGORIES;
  const revision = snapshot?.revision ?? task?.revision;
  const busy = isCheckupBusy(state);

  /**
   * 设施类别选择器：可选的大类来自后端能力表，界面不自己维护一份分类表。
   *
   * `scope` 是用户的勾选（只活在这一次会话里），`catalog.coreMajors` 是默认。核心口径
   * 进主请求；勾上的扩展大类不进主请求，而是等结果出来之后走按需补查 —— 把它们一起发
   * 过去会让"31 个小类 × 4 块 = 124 次"撞上默认 60 次的预算，那是一个还没发请求就会被
   * 拒的任务。
   */
  const catalog = view?.facilityCatalog ?? null;
  const [scope, setScope] = useState<MajorCategory[] | null>(null);
  const picked = useMemo(() => scope ?? catalog?.coreMajors ?? [], [scope, catalog]);
  const scopeParts = useMemo(
    () => (catalog ? splitScope(catalog, picked) : { core: [], extended: [] }),
    [catalog, picked]);
  const scopeMinors = useMemo(() => (catalog ? scopeParts.core.reduce(
    (total, key) => total + (catalog.majors.find(major => major.key === key)?.minorCategories ?? 0),
    0) : 0), [catalog, scopeParts.core]);
  const scopeFirstRoundPages = catalog ? estimatedFirstRoundPages(catalog, scopeParts.core) : null;
  const scopeMissingCore = catalog ? missingCoreMajors(catalog, scopeParts.core) : [];

  function toggleScope(key: MajorCategory, on: boolean) {
    if (catalog === null) return;
    setScope(on ? [...new Set([...picked, key])]
      : picked.filter(item => item !== key));
  }

  /** 刷新后把已有的补查读回来：结果是服务端的，不靠本地记。 */
  useEffect(() => {
    if (task !== undefined && isTerminal(task)) void controller.loadExtensions();
    // 只跟任务标识与状态走：补查自己的状态变化不该再触发一次列表读取。
  }, [controller, task?.taskId, task?.status]);

  /** 重试同理：本地不记"跑到哪了"，刷新后从服务端读回来，还在跑的那一轮直接接上。 */
  useEffect(() => {
    if (task !== undefined && isTerminal(task)) void controller.loadRetries();
  }, [controller, task?.taskId, task?.status]);

  /**
   * 图层：按当前修订按需取，取到就缓存（控制器按"图层:修订"记），**一次只取一层**。
   *
   * 写成"取一层、状态一变再取下一层"，而不是在循环里连着 await 五层：循环版本会在每次
   * 状态更新时被重启，于是同一层被取两遍；更糟的是先启动的那一圈在重启后被判成"作废"，
   * 它最后几层的失败会被静默吞掉 —— 表现是某一层永远画不出来，而且界面上一个字都不说。
   *
   * 失败按"层 + 修订"记账：同一版不反复重试，换了修订再试一次。
   */
  useEffect(() => {
    if (!snapshot || revision === undefined) return;
    const pending = MAP_LAYERS.find(id => (toggles[id] || id === 'facilities'
      || (Object.keys(HEAT_DEPENDENCIES) as HeatLayer[])
        .some(heat => toggles[heat] && HEAT_DEPENDENCIES[heat].includes(id)))
      && state.layers?.[id]?.revision !== revision && failed[id] !== revision);
    if (pending === undefined) return;
    controller.layer(pending).catch((error: unknown) => {
      setLayerErrors(previous => ({ ...previous, [pending]: error instanceof CheckupError
        ? error.message : '该图层未能加载' }));
      setFailed(previous => ({ ...previous, [pending]: revision }));
    });
  }, [controller, snapshot, state.phase, state.layers, revision, toggles, failed]);

  const drawables = useMemo(() => {
    const result: Partial<Record<LayerId, LayerDrawable>> = {};
    for (const id of MAP_LAYERS) {
      const layer = state.layers?.[id];
      // 修订对不上的一律不画：图上画着上一版的灰区、面板写着这一版的面积，是最难发现的错。
      if (!layer || layer.revision !== revision) continue;
      result[id] = drawableLayer(id, layer);
    }
    return result;
  }, [state.layers, revision]);

  /** 服务覆盖热力的输入：同样只认这一版的模型网格。 */
  const heatmapLayer = state.layers?.heatmap;
  const coverage = useMemo(() => heatmapLayer && heatmapLayer.revision === revision
    ? serviceSamples(heatmapLayer) : null, [heatmapLayer, revision]);

  /** 两种热力互斥（§8.1）：打开一种就关掉另一种，两张半透明画布叠在一起谁也读不清。 */
  function toggleHeat(heat: HeatLayer, on: boolean) {
    setToggles(current => ({ ...current, [heat]: on,
      ...(on ? { [heat === 'service' ? 'density' : 'service']: false } : {}) }));
  }

  const selectedPoint = useMemo(() => selected === null ? null
    : (drawables.facilities?.points ?? []).find(item => item.key === selected) ?? null,
  [drawables, selected]);

  const valid = lng !== null && lat !== null && Number.isFinite(lng) && Number.isFinite(lat)
    && lng >= -180 && lng <= 180 && lat > -85 && lat < 85;
  const engines = view?.engines ?? [];
  const selectedEngine = engines.find(item => item.engineId === engine) ?? null;
  const hybridEstimate = engine === 'osm_hybrid'
    ? hybridTimeEstimate(budget, view?.graphState ?? null) : null;
  const live = controller.hasLiveTask;
  /** 这一轮任务提交时的中心：结果、进行中的任务都按它画，不按选点草稿画。 */
  const taskCenter = snapshot?.center ?? state.input?.center;
  const draft = valid ? { lng: +lng!.toFixed(6), lat: +lat!.toFixed(6) } : null;
  const stale = taskCenter !== undefined && draft !== null
    && (taskCenter.lng !== draft.lng || taskCenter.lat !== draft.lat);
  const canStart = valid && !busy && !live && !!view && !!selectedEngine;

  // 换选点只改草稿，不取消、不清除任何任务：结果仍属于提交它的那个中心，直到用户重新体检。
  function choose(next: Center) {
    setCenter(next); setLng(next.lng); setLat(next.lat);
  }
  function edit(axis: 'lng' | 'lat', value: number | null) {
    const nextLng = axis === 'lng' ? value : lng;
    const nextLat = axis === 'lat' ? value : lat;
    if (axis === 'lng') setLng(value); else setLat(value);
    if (nextLng !== null && nextLat !== null && Number.isFinite(nextLng) && Number.isFinite(nextLat)
      && nextLng >= -180 && nextLng <= 180 && nextLat > -85 && nextLat < 85) {
      setCenter({ lng: +nextLng.toFixed(6), lat: +nextLat.toFixed(6) });
    }
  }
  function start() {
    if (!canStart || !draft) return;
    setCenter(draft); setSelected(null);
    setLayerErrors({}); setFailed({});
    // 勾选与默认口径一致时不带 `facilities`：请求体保持不变，幂等键也就不会因为一次
    // 多余的"我选了同样的三类"而换一个指纹。勾选变了才显式声明作用域。
    const scoped = catalog !== null && scopeParts.core.length > 0
      && !sameScope(scopeParts.core, catalog.coreMajors);
    void controller.start({ center: draft, engine, ...(budget === null ? {} : { budget }),
      ...(scoped ? { categories: scopeParts.core } : {}) });
  }
  function clear() {
    if (controller.clear()) { setSelected(null); setLayerErrors({}); setFailed({}); }
  }
  const sawLive = useRef(false);
  useEffect(() => {
    if (LIVE_PHASES.has(state.phase)) sawLive.current = true;
    // 明细到期时不要自动打开报告抽屉：那里已经不给报告了，弹出来只会让人以为出了问题。
    if (state.phase === 'completed' && sawLive.current) {
      sawLive.current = false;
      if (state.retained === undefined) setReportOpen(true);
    }
  }, [state.phase]);

  /** 水系标注与版本标识都只认当前这一版修订：换版就换，不留上一版的标注。 */
  const water = useMemo(() => waterView(snapshot?.water), [snapshot]);
  const version = useMemo(() => snapshot ? versionView(snapshot, view?.waterReviews ?? []) : null,
    [snapshot, view]);

  const clock = useClock(TICKING_PHASES.includes(state.phase));
  // 新的回答可能比上一次时钟跳动更晚到：以两者中较晚的为"此刻"，读数不会倒退成负数。
  const now = Math.max(clock, state.contact?.at ?? 0, state.reconnect?.since ?? 0);
  const liveNow = liveView(state, now);

  // §5 B2 决策 2：到期之后，明细一个字段都不再显示，只留结论与汇总。
  const retained = state.retained;
  const expiry = expiryNotice(retained?.retention);
  const expiresSoon = expiryLine(task?.retention);
  const retainedFacts = retained ? retainedSummaryLines(retained.summary) : [];

  const overall = snapshot?.report?.overall ?? snapshot?.scores?.overall ?? null;
  const items = useMemo(() => snapshot ? coverageItems(snapshot) : [], [snapshot]);
  const nearestGroups = useMemo(() => nearestFacilities(drawables.facilities, taskCenter ?? null,
    undefined, directory), [drawables.facilities, taskCenter, snapshot, view]);
  // §5 B2 决策 1 的判决：查到多少、达没达标、没达标的话下一步能不能靠重试解决。
  // 旧修订不报这一项，两个值都是 null，界面上就什么都不显示（不补一个 0% 出来）。
  const queryCoverage = snapshot ? queryCoverageLine(snapshot.facilities) : null;
  const queryCoverageNext = snapshot ? queryCoverageRetryLine(snapshot.facilities) : null;
  // 重试的入口只在"没达标"时出现：达标了还提示重试，就是在劝人多花额度。
  const retryRunning = state.retry !== undefined && !isRetryTerminal(state.retry.status);
  const weatherCenter = draft ?? taskCenter ?? null;
  const idle = state.phase === 'idle';

  const facts = task && <Descriptions className="wb-facts" size="small" column={1} items={[
    { key: 'task', label: '任务', children: <span data-testid="checkup-task-id">{task.taskId}</span> },
    { key: 'revision', label: '最新修订', children: <span data-testid="checkup-revision"
      data-revision={task.revision}>{`第 ${task.revision} 版`}</span> },
    { key: 'status', label: '状态', children: task.status +
      (task.businessStatus ? `（${BUSINESS_LABELS[task.businessStatus] ?? task.businessStatus}）` : '') },
    { key: 'spend', label: '已发请求', children: <span data-testid="checkup-requests"
      data-count={task.networkRequests}>{`${task.networkRequests} 次`}</span> },
    { key: 'tier', label: '等时圈档位', children: `${task.budget} 次上限` },
    ...(liveNow && liveNow.stage ? [{ key: 'stage', label: '当前阶段', children:
      <span data-testid="checkup-stage-now" data-stage={liveNow.stage}>
        {STAGE_LABELS[liveNow.stage] + (liveNow.stageFor === null ? ''
          : `（本阶段已 ${duration(liveNow.stageFor)}）`)}</span> }] : []),
    ...(liveNow && liveNow.step ? [{ key: 'step', label: '当前步骤', children:
      <span data-testid="checkup-step" data-step={liveNow.step.step}
        data-count={liveNow.step.count ?? ''}>
        {liveNow.step.label}{liveNow.stepCount ? ` · ${liveNow.stepCount}` : ''}
        {liveNow.stepFor === null ? '' : `（本步已 ${duration(liveNow.stepFor)}）`}</span> }] : []),
    ...(liveNow && liveNow.contactAgo !== null ? [{ key: 'contact', label: '最近一次成功连接',
      children: <span data-testid="checkup-contact" data-seconds={liveNow.contactAgo}>
        {`${duration(liveNow.contactAgo)}前`}</span> }] : []),
    ...(liveNow && (task.status === 'running' || task.status === 'cancelling') ? [{ key: 'activity',
      label: '服务端最近活动', children: <span data-testid="checkup-activity"
        data-seconds={liveNow.activityAgo ?? ''}>
        {liveNow.activityAgo === null ? '未记录'
          : `${duration(liveNow.activityAgo)}前`}</span> }] : []),
  ]} />;

  const floatingPanels = workspaceSize.width > 1080 && window.innerHeight >= 560;
  // Keep the requested size in state: collapsing the legend can restore the
  // panel's height, rather than permanently shrinking the user's preferred size.
  const visibleLayouts = {
    side: boundedLayout(panelLayouts.side, 'side', workspaceSize, floatingPanels ? legendHeight : 0),
    results: boundedLayout(panelLayouts.results, 'results', workspaceSize),
  };
  const legendStyle: CSSProperties | undefined = floatingPanels ? {
    left: visibleLayouts.side.x, top: visibleLayouts.side.y + visibleLayouts.side.height + LEGEND_GAP,
    right: 'auto', bottom: 'auto', width: visibleLayouts.side.width, maxWidth: 'none',
    '--legend-body-max-height': `${Math.min(240, Math.max(0, workspaceSize.height - 280 - 16 - LEGEND_GAP - LEGEND_BOTTOM_MARGIN - LEGEND_HEADER_ALLOWANCE))}px`,
  } as CSSProperties : undefined;
  const panelStyle = (panel: PanelId): CSSProperties | undefined => floatingPanels ? {
    left: visibleLayouts[panel].x,
    top: visibleLayouts[panel].y,
    width: visibleLayouts[panel].width,
    height: visibleLayouts[panel].height,
    zIndex: frontPanel === panel ? 12 : 10,
  } : undefined;

  function beginPanelInteraction(event: ReactPointerEvent<HTMLButtonElement>, panel: PanelId,
    mode: PanelInteraction['mode']) {
    if (event.button !== 0) return;
    event.preventDefault();
    setFrontPanel(panel);
    panelInteraction.current = { panel, mode, pointerId: event.pointerId,
      startX: event.clientX, startY: event.clientY, initial: visibleLayouts[panel] };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function movePanel(event: ReactPointerEvent<HTMLButtonElement>) {
    const interaction = panelInteraction.current;
    if (!interaction || interaction.pointerId !== event.pointerId) return;
    const deltaX = event.clientX - interaction.startX;
    const deltaY = event.clientY - interaction.startY;
    const next = interaction.mode === 'move'
      ? { ...interaction.initial, x: interaction.initial.x + deltaX, y: interaction.initial.y + deltaY }
      : { ...interaction.initial, width: interaction.initial.width + deltaX,
        height: interaction.initial.height + deltaY };
    setPanelLayouts(current => ({ ...current,
      [interaction.panel]: boundedLayout(next, interaction.panel, workspaceSize, legendHeight) }));
  }

  function endPanelInteraction(event: ReactPointerEvent<HTMLButtonElement>) {
    if (panelInteraction.current?.pointerId === event.pointerId) panelInteraction.current = null;
  }

  function nudgePanel(event: ReactKeyboardEvent<HTMLButtonElement>, panel: PanelId, mode: PanelInteraction['mode']) {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    setFrontPanel(panel);
    const step = event.shiftKey ? 24 : 8;
    setPanelLayouts(current => {
      const layout = boundedLayout(current[panel], panel, workspaceSize, legendHeight);
      const horizontal = event.key === 'ArrowRight' ? step : event.key === 'ArrowLeft' ? -step : 0;
      const vertical = event.key === 'ArrowDown' ? step : event.key === 'ArrowUp' ? -step : 0;
      const next = mode === 'move'
        ? { ...layout, x: layout.x + horizontal, y: layout.y + vertical }
        : { ...layout, width: layout.width + horizontal, height: layout.height + vertical };
      return { ...current, [panel]: boundedLayout(next, panel, workspaceSize, legendHeight) };
    });
  }

  const panelChrome = (panel: PanelId, label: string) => floatingPanels && <>
    <div className="wb-panel-chrome">
      <button type="button" className="wb-panel-move" data-testid={`panel-move-${panel}`}
        aria-label={`拖动${label}`} title="拖动面板"
        onPointerDown={event => beginPanelInteraction(event, panel, 'move')}
        onPointerMove={movePanel} onPointerUp={endPanelInteraction} onPointerCancel={endPanelInteraction}
        onKeyDown={event => nudgePanel(event, panel, 'move')}>
        <span className="wb-grip-dots" aria-hidden="true">
          {Array.from({ length: 6 }, (_, index) => <i key={index} />)}
        </span>
      </button>
    </div>
    <button type="button" className="wb-panel-resize" data-testid={`panel-resize-${panel}`}
      aria-label={`调整${label}大小`} title="拖动调整大小"
      onPointerDown={event => beginPanelInteraction(event, panel, 'resize')}
      onPointerMove={movePanel} onPointerUp={endPanelInteraction} onPointerCancel={endPanelInteraction}
      onKeyDown={event => nudgePanel(event, panel, 'resize')}><span aria-hidden="true" /></button>
  </>;

  return <main ref={workspaceRef} className="wb-body" data-stage={idle ? 'idle' : 'active'}>
    <aside className="wb-panel wb-side" style={panelStyle('side')} aria-label="体检条件">
      {panelChrome('side', '左侧面板')}
      <div className="wb-tabs" role="tablist" aria-label="体检设置">
        {TAB_KEYS.map(key => <button key={key} type="button" role="tab"
          id={`checkup-tab-${key}`} data-testid={`checkup-tab-${key}`}
          aria-selected={tab === key} aria-controls={`checkup-panel-${key}`}
          tabIndex={tab === key ? 0 : -1}
          className={tab === key ? 'wb-tab wb-tab-active' : 'wb-tab'}
          onClick={() => chooseTab(key)}>{TAB_LABELS[key]}</button>)}
      </div>
      <div className="wb-tabpanels">
        <div className="wb-scroll wb-tabpanel" role="tabpanel" id="checkup-panel-location"
          aria-labelledby="checkup-tab-location" hidden={tab !== 'location'}>
          <section className="wb-sec wb-setup">
            <LocationControls center={center} onPick={choose} />
            <div className="wb-coords">
              <InputNumber aria-label="经度" prefix="经度" value={lng}
                onChange={value => edit('lng', value)} precision={6} controls={false} />
              <InputNumber aria-label="纬度" prefix="纬度" value={lat}
                onChange={value => edit('lat', value)} precision={6} controls={false} />
            </div>
            {!valid && <Alert type="error" title="坐标无效：经度 −180～180，纬度 −85～85" />}
          </section>
        </div>

        <div className="wb-scroll wb-tabpanel" role="tabpanel" id="checkup-panel-engine"
          aria-labelledby="checkup-tab-engine" hidden={tab !== 'engine'}>
          <section className="wb-sec wb-engine">
            {algorithmSwitch}
            {capabilityError && <Alert type="error" title={capabilityError} showIcon />}
            <p className="wb-engine-name" data-testid="checkup-engine">引擎：{selectedEngine?.label
              ?? (view ? `${engine}（后端能力表未提供，无法提交）`
                : capabilityError ? '未能读取能力表，暂不能提交' : '正在读取能力表')}
              {selectedEngine?.engineVersion && <span className="wb-version">v{selectedEngine.engineVersion}</span>}</p>
            <label className="wb-field"><span>采样预算</span><Select aria-label="调用预算"
              value={budget ?? undefined} placeholder="—" onChange={setBudget}
              options={(selectedEngine?.budgets ?? []).map(value => ({
                value, label: `${value} 次`,
              }))} /></label>
            <p className="wb-hint">实际用时受网络影响</p>
            {hybridEstimate && <p className="wb-hint" data-testid="checkup-time-estimate">
              图已就绪 · 全程约 {hybridEstimate}
            </p>}
            <p className="wb-hint">步行 900 秒 · 服务标准 1000 米</p>
            {selectedEngine?.alert && <Alert type="warning" showIcon title={selectedEngine.alert} />}
            {selectedEngine?.caveat && selectedEngine.caveat !== selectedEngine.alert
              && <p className="wb-hint">{selectedEngine.caveat}</p>}

            {catalog !== null ? <div className="wb-scope" data-testid="checkup-scope">
              <p className="wb-label">设施类别</p>
              <p className="wb-hint">标"核心"的三类决定总体覆盖率；其余类别按需补查，
                只出点位与数量，不进入总体分。</p>
              {catalog.groups.map(group => <fieldset key={group.key}>
                <legend>{group.label}</legend>
                {group.majors.map(major => <Checkbox key={major.key}
                  data-testid={`checkup-scope-${major.key}`}
                  checked={picked.includes(major.key)}
                  onChange={event => toggleScope(major.key, event.target.checked)}>
                  {major.label}{major.core ? '（核心）' : ''}
                </Checkbox>)}
              </fieldset>)}
              {scopeFirstRoundPages !== null && <p className="wb-hint" data-testid="checkup-scope-budget">
                核心口径冷启动首轮约 {scopeFirstRoundPages} 页地点检索（按最多 {catalog.blocksUpperBound} 个
                查询分块 × {scopeMinors} 个检索小类估计）；实际分块由圈面决定，翻页、细分与重试
                还要更多页，命中缓存的页面不消耗网络额度。
              </p>}
              {scopeMissingCore.length > 0 && <Alert type="warning" showIcon
                data-testid="checkup-scope-missing"
                title={`总体覆盖率将无法给出：缺 ${scopeMissingCore.map(id => categoryLabel(id)).join('、')}`}
                description="总体区间分只按核心三类加权，不会用已分析的类别重新加权。" />}
              {scopeParts.extended.length > 0 && <p className="wb-hint" data-testid="checkup-scope-extended">
                待补查（结果出来后）：{scopeParts.extended.map(id => categoryLabel(id)).join('、')}
              </p>}
            </div> : view !== null ? <p className="wb-hint" data-testid="checkup-scope-unavailable">
              当前后端不提供设施类别目录，本次按后端的核心口径体检。
            </p> : null}
            {(view?.quota.label || (view?.quota.lines.length ?? 0) > 0) && <div className="wb-quota">
              {view?.quota.label && <p data-testid="quota-label">{view.quota.label}</p>}
              {view?.quota.lines.map(line => <p key={line}>{line}</p>)}
            </div>}
            {/* 检索计划的口径：本地处理上限与网络额度是两本账，首轮计划只是估算。
                旧后端缺哪一项就不显示哪一项，不写 0 也不写"(未配置)"。 */}
            {(view?.planningLines.length ?? 0) > 0 && <div className="wb-planning"
              data-testid="checkup-planning">
              {view?.planningLines.map(line => <p key={line}>{line}</p>)}
            </div>}
          </section>
        </div>

        <div className="wb-scroll wb-tabpanel" role="tabpanel" id="checkup-panel-layers"
          aria-labelledby="checkup-tab-layers" hidden={tab !== 'layers'}>
          <nav className="wb-dir" aria-label="图层与备注">
            <Fold title="图层" defaultOpen className="wb-layers">
              <p className="wb-group">热力 · 二选一</p>
              <div className="wb-layer">
                <Checkbox checked={!!toggles.service}
                  onChange={event => toggleHeat('service', event.target.checked)}>服务覆盖热力</Checkbox>
                {toggles.service && <Select className="wb-layer-select" size="small" aria-label="覆盖类别"
                  value={serviceMode} onChange={setServiceMode} options={serviceModes} popupMatchSelectWidth={false} />}
              </div>
              <div className="wb-layer">
                <Checkbox checked={!!toggles.density}
                  onChange={event => toggleHeat('density', event.target.checked)}>设施密度热力</Checkbox>
                {toggles.density && <Select className="wb-layer-select" size="small" aria-label="密度类别"
                  value={densityCategory} onChange={setDensityCategory} options={densityCategories}
                  popupMatchSelectWidth={false} />}
              </div>
              <p className="wb-group">叠加</p>
              <div className="wb-layer">
                <Checkbox checked={!!toggles.water}
                  onChange={event => setToggles({ ...toggles, water: event.target.checked })}>
                  <i className="api-swatch wb-swatch-water" />水系标注</Checkbox>
              </div>
              {MAP_LAYERS.map(id => <div className="wb-layer" key={id}>
                <Checkbox checked={!!toggles[id]}
                  onChange={event => setToggles({ ...toggles, [id]: event.target.checked })}>
                  <i className="api-swatch" style={layerSwatch(id)} />
                  {LAYER_STYLES[id].label}</Checkbox>
                {layerErrors[id] && <p className="wb-layer-error">{layerErrors[id]}</p>}
              </div>)}
            </Fold>
            <Fold title="备注" className="wb-notes">
              <dl>
                <dt>服务覆盖热力</dt>
                <dd>已覆盖按最近设施步行距离着色；灰色为服务不足，淡紫为未知。模型估计，不是实测。</dd>
                <dt>设施密度热力</dt>
                <dd>圈内设施等权计算，核半径 120 米；疑似重复只算一处，不代表覆盖率。</dd>
                <dt>水系标注</dt>
                <dd data-testid="water-layer-note">
                  {snapshot && !water.available ? '本次结果没有水系证据。'
                    : '已核实河道按实测宽度成面；错误底图水面与未裁决冲突单独标出。'}</dd>
                {MAP_LAYERS.map(id => <div key={id}><dt>{LAYER_STYLES[id].label}</dt>
                  <dd>{LAYER_STYLES[id].note}</dd></div>)}
              </dl>
            </Fold>
          </nav>
        </div>
      </div>
      <footer className="wb-actions">
        <Button aria-label="开始体检" type="primary" size="large" block onClick={start}
          disabled={!canStart}>开始体检</Button>
        {(live || (!busy && !idle)) && <div className="wb-actions-row">
          {live && <Button onClick={() => void controller.cancel()}
            disabled={state.phase === 'cancelling'}>取消任务</Button>}
          {!live && !busy && !idle && <Button onClick={clear}>清除结果</Button>}
        </div>}
        {live && <p className="wb-hint" data-testid="checkup-live-note">切换算法或刷新不会取消任务。</p>}
      </footer>
    </aside>

    <section className="wb-map api-map-section" data-testid="checkup-map-section"
      data-task-id={task?.taskId ?? ''} data-revision={revision ?? ''}
      data-water-reviews={version?.applied?.join(',') ?? ''}
      data-outdated={version && version.outdatedBy.length > 0 ? 'yes' : 'no'}
      data-drawn-revisions={[...new Set(MAP_LAYERS.filter(id => drawables[id])
        .map(id => state.layers?.[id]?.revision))].join(',')}>
      <CheckupMap center={center} onPick={choose} resultCenter={taskCenter}
        layers={toggles} drawables={drawables} coverage={coverage} serviceMode={serviceMode}
        categoryDirectory={directory} legendStyle={legendStyle} onLegendHeightChange={setLegendHeight}
        densityCategory={densityCategory} water={water}
        selectedId={selected} onSelect={setSelected} />
      <div className="wb-map-banners">
        {snapshot && stale && <Alert type="warning" showIcon
          title="中心已修改：地图与报告仍是上次体检的结果" />}
        {!snapshot && live && stale && <Alert type="info" showIcon
          title="进行中的任务仍按原中心计算" />}
        {version && version.outdatedBy.length > 0 && <Alert type="warning" showIcon
          data-testid="checkup-outdated" title={`旧版本（第 ${snapshot?.revision} 版）：水系数据已修订`}
          description={outdatedText(version)} />}
      </div>
    </section>

    <aside className="wb-panel wb-results" style={panelStyle('results')} aria-label="体检报告">
      {panelChrome('results', '右侧面板')}
      <div className="wb-scroll">
        {!idle ? <>
        <section className="wb-sec wb-status">
          <div className="wb-sec-head">
            <span className="wb-phase" data-testid="checkup-phase"
              data-phase={state.phase} data-tone={PHASE_TONE[state.phase] ?? 'live'}>
              {PHASE_LABELS[state.phase] ?? state.phase}</span>
            {task && <b className="wb-clock" data-testid="checkup-elapsed"
              data-seconds={liveNow ? liveNow.elapsed ?? '' : task.elapsedSeconds}>
              {liveNow ? liveNow.kind === 'queued' ? `尚未开始（已排队 ${duration(liveNow.queued)}）`
                : liveNow.elapsed === null ? '尚未开始' : duration(liveNow.elapsed)
                : `${task.elapsedSeconds.toFixed(1)} 秒`}</b>}
          </div>
          {task && <StageProgress stage={task.stage} />}
          {liveNow && <div className="wb-live" role="status" data-testid="checkup-live"
            data-kind={liveNow.kind} data-tone={LIVE_ALERT[liveNow.kind]}>
            <strong>{liveNow.title}</strong>
            <p>{liveNow.hint + (liveNow.kind === 'restoring' && state.input
              ? `（请求标识 ${state.input.clientRequestId}）` : '')}</p>
          </div>}
          {state.phase === 'cancelled' && <Alert type="info" title="任务已取消"
            description="已发请求仍计入预算。" />}
          <CompletionSummary value={task?.completion} />
          {task?.completion?.canContinue && !busy && <Button block type="primary"
            data-testid="checkup-continue" disabled={stale}
            onClick={() => void controller.continueReport()}>
            {continuationLabel(task.completion.restartRetrieval, view?.poiRoundLimit)}
          </Button>}
          {state.error && <Alert type="error" title={state.error} showIcon data-testid="checkup-error"
            action={<Button aria-label="重试" size="small"
              onClick={() => void controller.retry()}>{state.recovery === 'unconfirmed' ? '重新提交'
                : state.recovery === 'expired' || task?.status === 'failed' ? '重新体检' : '重试'}</Button>} />}
        </section>

        {/* 扩展设施补查：独立一轮，只加设施，不改上面的报告与评分。 */}
        {task !== undefined && isTerminal(task) && catalog !== null && <>
          <section className="wb-sec wb-extension" aria-label="扩展设施补查">
            <p className="wb-label">扩展设施补查</p>
            {scopeParts.extended.length === 0
              ? <p className="wb-hint" data-testid="checkup-extension-none">
                  在"采样与引擎"里勾选核心三类之外的类别，就能在这里补查。
                </p>
              : <>
                <p className="wb-hint">复用本次已经算出的圈面，只再检索这几类；
                  它不发布新修订，也不进入总体覆盖率。</p>
                <Button size="small" data-testid="checkup-extension-run"
                  disabled={state.extensionRunning === true}
                  onClick={() => void controller.extend(scopeParts.extended)}>
                  {state.extensionRunning === true ? '检索中…'
                    : `补查 ${scopeParts.extended.map(id => categoryLabel(id)).join('、')}`}
                </Button>
              </>}
            {state.extensionError && <Alert type="error" showIcon data-testid="checkup-extension-error"
              title={state.extensionError} />}
            {(state.extensions?.length ?? 0) > 0 && <ul className="wb-extension-list"
              data-testid="checkup-extensions">
              {state.extensions?.map(item => <li key={item.extensionId} data-status={item.status}>
                <b>{item.categories.map(id => categoryLabel(id)).join('、')}</b>
                {' · '}{EXTENSION_STATUS_LABELS[item.status] ?? item.status}
                {Object.keys(item.countsByCategory).length > 0 && <span className="wb-extension-counts">
                  {' · '}{Object.entries(item.countsByCategory)
                    .map(([key, value]) => `${categoryLabel(key)} ${value} 处`).join('，')}
                </span>}
                {/* 两本账分开报：页面处理含缓存重放，新增网络才是花掉额度的部分。
                    后端没记的项（旧记录）整行省略，不补 0。 */}
                {extensionLines(item).map(line => <p key={line} className="wb-extension-facts"
                  data-testid="checkup-extension-facts">{line}</p>)}
                {!EXTENSION_DONE.has(item.status) && <Button type="link" size="small"
                  onClick={() => void controller.cancelExtension(item.extensionId)}>取消</Button>}
              </li>)}
            </ul>}
          </section>
        </>}

        {snapshot && <section className="wb-sec wb-summary" aria-label="覆盖区间摘要">
          <p className="wb-label">{snapshot.completion?.evaluationStatus === 'partial' ? '阶段性总体覆盖区间' : '覆盖率'}</p>
          {overall && overall.available ? <>
            <p className="wb-range"><b>{fixed(overall.coverageLowerPct)}</b><span>～</span>
              <b>{fixed(overall.coverageUpperPct)}</b><small>%</small></p>
            <RangeMeter lower={overall.coverageLowerPct} upper={overall.coverageUpperPct} />
            <p className="wb-meter-key"><i className="k-known" />已知覆盖<i className="k-unknown" />未知
              <span>可评估 {percent(overall.assessablePct)}</span></p>
          </> : <Alert type="warning" showIcon
            title="总体区间暂不给出" description={overallUnavailableText(snapshot)} />}
          <ul className="wb-cats">{items.map(item => <li key={item.category}>
            <span className="wb-cat-name"><i style={{ background: item.color }} />{item.label}</span>
            <RangeMeter lower={item.lowerPct} upper={item.upperPct} color={item.color} />
            <span className="wb-cat-value">{item.supported
              ? item.assessablePct === 0 ? '全部未知' : `${fixed(item.lowerPct)}–${fixed(item.upperPct)}%` : '无法确定'}</span>
          </li>)}</ul>
          <Button block type="primary" ghost onClick={() => setReportOpen(true)}>查看体检报告</Button>
        </section>}

        {snapshot && <section className="wb-sec wb-nearest" data-testid="checkup-nearest">
          <h2 className="wb-h">周边设施 · 每类最近 5 处</h2>
          {queryCoverage && <p className="wb-hint"
            data-testid="checkup-query-coverage">{queryCoverage}</p>}
          {expiresSoon && <p className="wb-hint"
            data-testid="checkup-expiry-line">{expiresSoon}</p>}
          {queryCoverageNext && <div className="wb-retry" data-testid="checkup-retry">
            <p className="wb-hint" data-testid="checkup-query-coverage-next">
              {queryCoverageNext}</p>
            <Space size="small" wrap>
              <Button size="small" data-testid="checkup-retry-run"
                disabled={state.retrying === true}
                onClick={() => void controller.retryCheckup()}>
                {retryActionLabel(state.retrying === true)}</Button>
              {retryRunning && <Button size="small" data-testid="checkup-retry-cancel"
                onClick={() => void controller.cancelRetry(state.retry!.retryId)}>取消</Button>}
            </Space>
            {state.retry && retryLines(state.retry).map(line =>
              <p key={line} className="wb-extension-facts"
                data-testid="checkup-retry-facts">{line}</p>)}
            {state.retryError && <Alert type="error" showIcon data-testid="checkup-retry-error"
              title={state.retryError} />}
          </div>}
          {!drawables.facilities ? <p className="wb-hint">{layerErrors.facilities
            ?? '设施结果尚未加载。'}</p>
            : drawables.facilities.state === 'empty' ? <p className="wb-hint"
              data-testid="checkup-nearest-empty">{nearestEmptyNote(snapshot.facilities)}</p>
            : <>
              <p className="wb-hint">按直线距离排序；点选可查步行路线。</p>
              {nearestGroups.map(group => <div className="wb-near" key={group.category}>
                <p className="wb-near-head"><i style={{ background: group.color }} />{group.label}
                  <span>共 {group.total} 处</span></p>
                {group.facilities.length === 0 ? <p className="wb-hint">本次没有{group.label}设施。</p>
                  : <ol className="wb-near-list">{group.facilities.map(facility => <li key={facility.key}>
                    <button type="button" data-testid={`nearest-${facility.key}`}
                      data-selected={selected === facility.key ? 'yes' : 'no'}
                      onClick={() => setSelected(facility.key)}>
                      <span className="wb-near-name">{facility.name}</span>
                      <span className="wb-near-distance">{facility.distanceM < 1000
                        ? `${facility.distanceM.toFixed(0)} 米`
                        : `${(facility.distanceM / 1000).toFixed(1)} 公里`}</span>
                      {facility.address && <span className="wb-near-address">{facility.address}</span>}
                    </button>
                  </li>)}</ol>}
              </div>)}
            </>}
        </section>}

        {snapshot && (selectedPoint || state.route || state.routeError) && <section className="wb-sec wb-facility">
          <p className="wb-label">设施</p>
          {selectedPoint && <>
            <p className="wb-facility-title">{selectedPoint.title}</p>
            <Button size="small" onClick={() => void controller.detail(selectedPoint.key)}
              disabled={busy}>查询步行路线</Button>
          </>}
          <RouteDetail route={state.route} error={state.routeError}
            facilityName={selectedPoint && typeof selectedPoint.properties.name === 'string'
              && selectedPoint.properties.name.length > 0 ? selectedPoint.properties.name : null} />
        </section>}

        {expiry && <section className="wb-sec wb-expired" data-testid="checkup-expired">
          <h2 className="wb-h">这次体检的明细已到期</h2>
          <p className="wb-hint" data-testid="checkup-expired-notice">{expiry}</p>
          {retained && <>
            <p className="wb-hint" data-testid="checkup-retained-revision">
              {`保留的是第 ${retained.revision} 版（${retained.stage}）的定稿汇总。`}</p>
            {retainedFacts.length > 0 && <ul className="wb-expired-facts"
              data-testid="checkup-retained-facts">
              {retainedFacts.map(line => <li key={line} className="wb-hint">{line}</li>)}
            </ul>}
            {retained.notes.map(note => <p key={note} className="wb-hint"
              data-testid="checkup-retained-note">{note}</p>)}
          </>}
          {!retained && <p className="wb-hint" data-testid="checkup-retained-missing">
            这一版没有留存汇总，只有任务状态可用；明细按保留期不再提供。</p>}
        </section>}

        {task && <Fold title="任务信息" className="wb-task">
          {facts}
          {state.input && <p className="wb-hint wb-center">
            中心 {state.input.center.lng.toFixed(6)}, {state.input.center.lat.toFixed(6)}</p>}
          {version && snapshot?.taskId === task.taskId && <p className="wb-hint" data-testid="checkup-version"
            data-recomputed={version.recomputed ? 'yes' : 'no'}>
            {`结果版本：第 ${snapshot.revision} 版 · 水系 `
              + (version.applied === null ? '早于水系复核' : version.applied.length === 0 ? 'OSM 原样（未采用复核）'
                : version.applied.join('、'))}
            {version.recomputed && <><br />{recomputedText(version.recomputed)}</>}</p>}
        </Fold>}
        </> : <section className="wb-sec wb-empty-report"><p className="wb-hint">暂无体检结果</p></section>}
        <div className="wb-weather-bottom"><WeatherCard center={weatherCenter} /></div>
      </div>
    </aside>

    <Drawer title="体检报告" open={reportOpen} onClose={() => setReportOpen(false)} size={960}
      rootClassName="rp-drawer">
      {snapshot ? <CheckupReport snapshot={snapshot} stale={stale} waterReviews={view?.waterReviews}
        continuation={task?.completion} continuationPoiLimit={view?.poiRoundLimit} busy={busy}
        onContinue={() => void controller.continueReport()} onCancel={() => void controller.cancel()} />
        : retained ? <p className="api-muted" data-testid="checkup-report-expired">
            这次体检的明细已到期，报告不再提供；结论与汇总在左侧"明细已到期"一栏。</p>
        : <p className="api-muted">体检完成后在这里显示报告。</p>}
    </Drawer>
  </main>;
}
