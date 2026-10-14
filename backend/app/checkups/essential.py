"""Three named services, independent of major-category scores and route overrides."""
from shapely.geometry import Point
from shapely.ops import unary_union

from .. import catalog
from ..accessibility.zones import merge_zones
from .models import AccessibilityEvidence, EssentialServicesEvidence, HeatmapEvidence, ServiceGapsEvidence


def matches(item, profile):
    category = catalog.contract_key(item.get('category') or '')
    if category not in profile['categories']:
        return False
    if any(hint in item.get('name', '') for hint in profile.get('excludeNameHints', [])):
        return False
    if category == 'combined_school':
        text = ' '.join([item.get('name', ''), *item.get('sourceTags', [])])
        return ('primary_education' in catalog.capabilities_for(category)
                and any(hint in text for hint in profile['combinedPrimaryHints']))
    return True


def assess_essential(*, context, facilities, store, views, domain, excluded,
                     obstacles, attachments, rule, progress):
    from . import accessibility_stage as stage
    rows, evidence_notes = [], []
    incomplete = context.get('queryIncompleteRegions')
    coverage = context.get('queryCoverage') or []
    padding = (context.get('queryDomain') or {}).get('paddingMeters', 0)
    for index, profile in enumerate(catalog.ESSENTIAL_SERVICES, 1):
        keys = set(profile['queryCategories'])
        observed = {catalog.contract_key(row.get('category', '')) for row in coverage
                    if catalog.contract_key(row.get('category', '')) in catalog.BY_KEY
                    if row.get('query') == catalog.BY_KEY[catalog.contract_key(row.get('category', ''))].query}
        # A category absent from the plan cannot look like a completed empty query.
        admitted = keys <= observed and padding >= stage.service_rules.QUERY_PADDING_M
        complete = context.get('queryStatus') == 'completed'
        masks = []
        if not admitted or (incomplete is None and not complete):
            masks.append(domain)
        for category, regions in (incomplete or {}).items():
            if catalog.contract_key(category) in keys:
                masks.extend(stage.metric_region(region, store.projection) for region in regions)
        # Conflicting classifications and invalid target records block absence locally.
        unresolved = [f for f in facilities if catalog.contract_key(f.get('category', '')) in profile['categories']
                      and not matches(f, profile)]
        for item in context.get('reviewCandidates', []) + context.get('quarantine', []) + unresolved:
            provenance = item.get('provenance') or []
            if isinstance(provenance, dict):
                provenance = [provenance]
            relevant = catalog.contract_key(item.get('category', '')) in profile['categories'] or any(
                catalog.contract_key(p.get('category', '')) in keys for p in provenance)
            if not relevant:
                continue
            location = item.get('location')
            try:
                masks.append(Point(*store.projection.origin((location['lng'], location['lat']))))
            except (KeyError, TypeError, ValueError):
                masks.append(domain)
        mask = unary_union(masks) if masks else None
        progress('essential_category', major=profile['label'], index=index, total=3)
        row = stage.evaluate_category(
            profile['key'], facilities, store=store, views=views, domain=domain, rule=rule,
            query_complete=admitted and (complete or incomplete is not None), obstacles=obstacles,
            attachments=attachments, facility_filter=lambda f, p=profile: matches(f, p),
            confirmed_empty=True, query_incomplete=mask,
            on_cell=lambda count: progress('essential_category', cells=count,
                                          major=profile['label'], index=index, total=3))
        if row.supported and not stage._areas_match(row.areas, domain.area):
            row.supported, row.unavailable_reason = False, stage.AREA_MISMATCH
        rows.append(row)
        if not admitted:
            evidence_notes.append(profile['label'] + '：相关小类未检索或服务查询范围不足，缺失不作结论。')
    records = [record for row in rows if row.supported for record in row.records]
    zones = merge_zones(records, domain=domain, majors=[p['key'] for p in catalog.ESSENTIAL_SERVICES],
                        obstacle_between=obstacles.separates if obstacles.available else None)
    public_domain = stage.public_geometry(store.projection, domain)
    status = 'complete' if (obstacles.available and all(r.supported and r.areas.unknown_m2 < 1 for r in rows)) else 'partial'
    notes = ['专项仅检查菜市场、独立药店及小学；不进入三大类总体分。',
             '未检索到只表示当前查询范围与主关键词下的疑似缺口，目录未经独立核实。',
             '有候选但入口未解决、相关检索未完成或路网证据不足时保留未知。',
             '专项为模型推定；大类路线抽检不构成专项核验。', *evidence_notes]
    return EssentialServicesEvidence(
        directory=list(catalog.ESSENTIAL_SERVICES),
        accessibility=AccessibilityEvidence(status=status, domain=public_domain,
            domain_area_m2=domain.area, excluded_area_m2=excluded, views=views.counts,
            categories=[stage._category_evidence(row) for row in rows], notes=notes),
        service_gaps=ServiceGapsEvidence(status=status,
            zones=[stage._zone_payload(zone, store.projection) for zone in zones],
            by_category_m2=stage._zone_area_by_category(zones),
            gap_area_m2=stage._union_gap_area(records, domain),
            composite_area_m2=sum(z.area_m2 for z in zones if z.kind == 'composite'),
            obstacle_layer_available=obstacles.available, notes=notes),
        heatmap=HeatmapEvidence(domain=public_domain,
            categories={row.category: list(row.heat) for row in rows}, notes=notes), notes=notes)
