import hashlib
import json
import math

from life_circle.coordinates import LocalProjection

from ..catalog import poi_rules

# The POI runtime's view of the one category dictionary, under its own names.
RULES = poi_rules()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def sequence(tile_id, x, y, size, category, query, projection):
    """One ``around`` search over one square block.

    The single definition of what a query sequence is, shared by the fixed plan
    below and by the adaptive planner: the block's circumscribed circle is the
    search, and the caller clips the result to what it actually needs.
    """
    center = projection.to_geographic((x+size/2, y+size/2))
    return {'sequenceId': f'{tile_id}:{category}:{query}', 'tileId': tile_id,
        'category': category, 'query': query, 'center': list(center),
        'radius': math.ceil(size/math.sqrt(2))+5, 'localMeters': [x, y, x+size, y+size],
        'bd09ll': [*projection.to_geographic((x, y)), *projection.to_geographic((x+size, y+size))]}


def build_plan(request, config):
    if set(request.categories) - config.category_budgets.keys():
        raise ValueError('explicit budget for every requested category required')
    origin = (request.center.lng, request.center.lat)
    projection = LocalProjection(origin)
    def extent(half):
        southwest = projection.to_geographic((-half, -half))
        northeast = projection.to_geographic((half, half))
        if not (-180 <= southwest[0] < northeast[0] <= 180 and -85 < southwest[1] < northeast[1] < 85):
            raise ValueError('window outside supported projection')
        return {'localMeters': [-half, -half, half, half], 'bd09ll': [*southwest, *northeast]}
    half = request.analysis_half_width_meters + request.search_margin_meters
    size = half / 2
    sequences = []
    for row in range(4):
        for col in range(4):
            x, y = -half + col*size, -half + row*size
            for category in request.categories:
                for query in RULES['queries'][category]:
                    sequences.append(sequence(f'r{row}c{col}', x, y, size, category, query, projection))
    if config.phase == 'smoke':
        sequences = [s for s in sequences if s['tileId'] == 'r0c0' and s['query'] == RULES['queries'][s['category']][0]]
    plan = {'provider': 'baidu_place', 'apiVersion': '3.0', 'coordinateSystem': 'bd09ll',
        'projectionVersion': 'local-equirectangular-6371008.8-v1', 'ruleVersion': RULES['version'],
        'analysisExtent': extent(request.analysis_half_width_meters), 'searchExtent': extent(half),
        'pageSize': 20, 'maxPages': 8, 'sequences': sequences, 'order': 'first-pages-then-round-robin',
        'request': request.model_dump(mode='json', by_alias=True), 'rulesHash': digest(RULES)}
    runtime = config.model_dump(mode='json', by_alias=True, exclude={'authorized', 'approved_config_hash'})
    plan['configHash'] = digest({'plan': plan, 'runtime': runtime})
    plan['requestUpperBound'] = min(len(sequences)*8*2, config.total_budget,
                                     sum(config.category_budgets[c] for c in request.categories))
    return plan


def parameters(sequence, page):
    lng, lat = sequence['center']
    return {'query': sequence['query'], 'location': f'{lat:.6f},{lng:.6f}', 'radius': sequence['radius'],
            'coord_type': 3, 'radius_limit': 'true', 'scope': 2, 'page_size': 20, 'page_num': page, 'output': 'json'}
