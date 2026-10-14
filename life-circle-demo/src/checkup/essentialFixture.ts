import type { EssentialServicesEvidence } from './contract';

/** Synthetic fixture, never imported by the application entry point. */
export function essentialFixture(): EssentialServicesEvidence {
  return { ruleVersion: 'essential-services-v1', catalogCompleteness: 'unverified', verificationStatus: 'model_only',
    directory: ['market', 'pharmacy', 'primary_school'].map(key => ({ key, label: key })), notes: ['模型推定'],
    accessibility: { status: 'partial', domain: null, domainAreaM2: 1000, excludedAreaM2: 0,
      gridStepM: 50, refinedStepM: 25, maxLeafCells: 10000, searchCutoffM: 1100, views: {}, notes: [],
      categories: ['market', 'pharmacy', 'primary_school'].map(category => ({ category, supported: true,
        coveredM2: 250, gapM2: 500, unknownM2: 250, domainAreaM2: 1000, cells: {}, entrances: {},
        unavailableReason: null, coverage: null })) },
    serviceGaps: { status: 'partial', zones: [], byCategoryM2: {}, compositeAreaM2: 0, gapAreaM2: 0,
      compositeMinCategories: 2, minLabelAreaM2: 2500, obstacleLayerAvailable: true, notes: [] },
    heatmap: { metric: 'walking_route', estimated: true, stepM: 50, domain: null, categories: {}, notes: [] } };
}
