"""Report tolerance for spatial membership; never mixes contour timing into accuracy."""
import math
from collections import Counter


def tolerant_metrics(plan, strict):
    from tools.validate_hybrid import wilson
    rows = []
    for row in strict['cases']:
        label = row['classification']
        duration = row['evidence'].get('duration')
        tolerant = ('within_tolerance' if label in ('fp', 'fn') and duration is not None
                    and math.isfinite(duration) and 885 <= duration <= 915 else label)
        rows.append({**row, 'strict_classification': label, 'classification': tolerant})

    def summarize(cases):
        c = Counter(r['classification'] for r in cases)
        n = sum(c[k] for k in ('tp', 'tn', 'fp', 'fn', 'within_tolerance'))
        correct = c['tp'] + c['tn'] + c['within_tolerance']
        predicted_inside = sum(r['prediction'] is True and not r['classification'].endswith('unknown') for r in cases)
        truly_inside = sum(r['evidence'].get('reachable') is True and r['prediction'] is not None for r in cases)
        divide = lambda a, b: a/b if b else None
        return dict(planned=len(cases), decidable=n,
                    **{k: c[k] for k in ('tp', 'tn', 'fp', 'fn', 'within_tolerance', 'api_unknown', 'algorithm_unknown')},
                    accuracy=divide(correct, n), accuracy_wilson_95=wilson(correct, n),
                    false_inclusion_rate=divide(c['fp'], predicted_inside), false_exclusion_rate=divide(c['fn'], truly_inside),
                    false_inclusion_wilson_95=wilson(c['fp'], predicted_inside),
                    false_exclusion_wilson_95=wilson(c['fn'], truly_inside),
                    unknown_fraction=divide(len(cases)-n, len(cases)))
    overall = summarize(rows)
    strata = {g: summarize([r for r in rows if r['group'] == g]) for g in plan['allocations']}
    enough = strict['outcome'] != 'insufficient_evidence'
    passed = (overall['accuracy'] or 0) >= .9 and (strata['boundary']['accuracy'] or 0) >= .95
    return {**strict, 'schema_version': 'hybrid-validation-v3', 'cases': rows, 'overall': overall, 'strata': strata,
            'strict_overall': strict['overall'], 'strict_strata': strict['strata'],
            'outcome': 'insufficient_evidence' if not enough else 'passed' if passed else 'failed',
            'scope': 'spatial_membership_with_15s_tolerance_not_contour_duration_or_global_boundary_error'}
