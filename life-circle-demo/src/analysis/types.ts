import type { Center } from '../types';
import type { TaskResultResponse as ContractTaskResult, TaskStatusResponse as ContractTaskStatus } from '../api-contract';

export type Budget = 200 | 400 | 800;
export type BusinessStatus = 'complete' | 'partial' | 'failed' | 'empty';
export type AnalysisInput = { center: Center; budget: Budget; clientRequestId: string };
export type DataSource = 'synthetic' | 'baidu_walking';
export type BusinessGeometry = {
  type: 'MultiPolygon'; coordinateSystem: 'bd09ll'; coordinates: [number, number][][][];
};
export type TaskStatus = ContractTaskStatus;
export type Isochrone = {
  validationStatus?: 'not_independently_validated' | 'independently_validated';
  evidenceGeometry?: BusinessGeometry;
  inferredRegion?: BusinessGeometry;
  timeBands?: { minutes: number; geometry: BusinessGeometry | null }[];
  unreachableRegion?: BusinessGeometry | null;
  coordinateSystem: 'bd09ll'; geometry: BusinessGeometry | null;
  uncertainRegion: BusinessGeometry; unknownRegion: BusinessGeometry; computationExtent: BusinessGeometry;
  quality: 'usable' | 'partial' | 'insufficient'; stopReason: string; warnings: string[];
  statistics: { requests: number; network_requests: number; retries: number; unknown_area: number;
    unfinished_boundary: number; total_seconds: number; failures: Record<string, number>;
    matrix_route_pairs?: number; detailed_route_requests?: number; sends?: number; responses?: number; terminations?: number;
    transport_failures?: number; cancellations?: number; endpoint_invalid?: number;
    unfinished_boundary_length_m?: number };
  config: { origin: [number, number]; budget: number; seed: number; [key: string]: unknown };
};
export type AnalysisResult = Omit<ContractTaskResult, 'isochrone' | 'facilityAnalysis'> & { center: Center; isochrone: Isochrone; facilityAnalysis?: ContractTaskResult['facilityAnalysis'] };
export interface AnalysisService {
  create(input: AnalysisInput): Promise<TaskStatus>;
  status(id: string, signal?: AbortSignal): Promise<TaskStatus>;
  result(id: string, signal?: AbortSignal): Promise<AnalysisResult>;
  cancel(id: string): Promise<TaskStatus>;
  cancelByRequest(clientRequestId: string): Promise<TaskStatus>;
}
export type AnalysisState = {
  phase: 'idle' | 'submitting' | 'running' | 'fetching' | 'cancelling' | 'completed' | 'cancelled' | 'error';
  task?: TaskStatus; result?: AnalysisResult; error?: string;
};

export function isAnalysisBusy(state: AnalysisState) {
  return ['submitting', 'running', 'fetching', 'cancelling'].includes(state.phase);
}
