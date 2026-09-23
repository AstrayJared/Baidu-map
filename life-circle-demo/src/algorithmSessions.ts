import type { Center } from './types';
import type { AnalysisResult, Budget } from './analysis/types';
import type { Layers } from './analysis/ApiMap';
import type { HybridResultResponse } from './api-contract';

type ApiRoute = { taskId: string; points: [number, number][] };

export type BaiduSession = {
  center: Center; lng: number | null; lat: number | null; budget: Budget;
  group: string; selected: string | null; showFacilities: boolean; showAssessments: boolean;
  route: ApiRoute | null; lastResult?: AnalysisResult; dirty: boolean; reportOpen: boolean; layers: Layers;
};

export type HybridSession = {
  center: Center; lng: number | null; lat: number | null; budget: number;
  result?: HybridResultResponse; dirty: boolean; layers: Layers;
};

const baiduDefaults: BaiduSession = {
  center: { lng: 116.404, lat: 39.915 }, lng: 116.404, lat: 39.915, budget: 400,
  group: 'all', selected: null, showFacilities: true, showAssessments: true, route: null,
  dirty: false, reportOpen: false,
  layers: { reachable: true, unreachable: true, unknown: true, uncertain: true, extent: false, serviceBlind: true },
};
const hybridDefaults: HybridSession = {
  center: { lng: 121.513925, lat: 31.313079 }, lng: 121.513925, lat: 31.313079, budget: 400, dirty: false,
  layers: { reachable: true, unreachable: false, unknown: false, uncertain: false, extent: false, serviceBlind: false },
};

let baiduSession: BaiduSession = baiduDefaults;
let hybridSession: HybridSession = hybridDefaults;

export const getBaiduSession = () => baiduSession;
export const getHybridSession = () => hybridSession;
export const saveBaiduSession = (next: BaiduSession) => { baiduSession = next; };
export const saveHybridSession = (next: HybridSession) => { hybridSession = next; };
