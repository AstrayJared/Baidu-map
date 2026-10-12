"""Deterministic classification and UID evidence aggregation, never synthetic repair."""
from collections import defaultdict
from copy import deepcopy
import math
import re
import unicodedata

from life_circle.coordinates import LocalProjection
from .planner import RULES, digest

DECIMAL = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)\Z')


def coordinate(value, lower, upper):
    if isinstance(value, str):
        value = value.strip()
        if not DECIMAL.fullmatch(value):
            raise ValueError('invalid_coordinate')
        value = float(value)
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError('invalid_coordinate')
    return float(value)


def location(value):
    if not isinstance(value, dict):
        raise ValueError('missing_location')
    return {'lng': coordinate(value.get('lng'), -180, 180), 'lat': coordinate(value.get('lat'), -90, 90)}


def text(value):
    return value.strip() if isinstance(value, str) else ''


def classify(name, tags):
    from ..classification import classify as shared_classify
    from ..catalog import poi_key
    category, status, evidence = shared_classify(name, tags, require_tag=True)
    return None if category is None else poi_key(category), status, evidence


def normalize(row, provenance, source):
    uid, name = text(row.get('uid')), text(row.get('name'))
    if not uid or not name:
        raise ValueError('missing_uid_or_name')
    point = location(row.get('location'))
    details = row.get('detail_info') or {}
    tags = [text(details.get('classified_poi_tag'))] if text(details.get('classified_poi_tag')) else []
    category, status, evidence = classify(name, tags)
    # §4.3/§5.3: the raw point and the provider's navigation point are entrance
    # *candidates*. ``navi_location`` is guidance and may be a pickup point, so it
    # is never recorded as a walking entrance. No place query establishes an
    # entrance, so ``confirmedEntrances`` stays null — an empty list would claim
    # the opposite, that there is confirmed to be none.
    navigation, warnings = None, []
    if details.get('navi_location') is not None:
        try:
            navigation = location(details['navi_location'])
        except ValueError:
            warnings.append('invalid_navigation_location')
    return {'id': f'{source}:{uid}', 'source': source, 'sourceUid': uid, 'name': name,
        'category': category, 'coordinateSystem': 'bd09ll', 'location': point,
        'navigationLocation': navigation, 'confirmedEntrances': None,
        'parentUid': text(details.get('parent_id')) or None,
        'address': text(row.get('address')), 'sourceTags': tags, 'operatingStatus': 'unknown',
        'classificationStatus': status, 'classificationRuleVersion': RULES['version'],
        'classificationEvidence': evidence, 'possibleDuplicateGroup': None,
        'provenance': [provenance], 'warnings': warnings}


def inside(point, bounds, projection):
    x, y = projection.to_local((point['lng'], point['lat']))
    # Only compensate for floating-point inversion at exact window edges (< 1 micrometre).
    epsilon = 1e-6
    return bounds[0]-epsilon <= x <= bounds[2]+epsilon and bounds[1]-epsilon <= y <= bounds[3]+epsilon


def merge_entities(records, request, plan=None, *, within=None, nearby=None):
    """Merge the records of one run into entities, each with its own verdict.

    ``plan`` names the rectangular search window a POI runtime ran, and every
    entity outside it is reported rather than merged. A caller whose counting
    region is not a rectangle — the checkup counts inside the computed boundary —
    passes ``within`` instead: one test from a geographic point to whether this
    run counts there. Exactly one of the two is required.

    ``nearby``, when given, is a list that receives the *accepted* entities outside
    the counting region, classified exactly like the counted ones: they are never
    counted, but a facility just across the boundary still serves the places inside
    it. The outside report is unchanged.
    """
    if within is None and plan is None:
        raise ValueError('a merge needs a counting region: plan or within')
    by_uid = defaultdict(list)
    for record in records:
        by_uid[record['id']].append(record)
    projection = LocalProjection((request.center.lng, request.center.lat))
    counted = (within if within is not None else
               (lambda point: inside(point, plan['searchExtent']['localMeters'], projection)))
    accepted, review, excluded, outside = [], [], [], []
    for uid, values in sorted(by_uid.items()):
        observations = sorted({digest({k: v for k, v in r.items() if k != 'provenance'}): r for r in values}.values(),
                              key=lambda r: digest({k: v for k, v in r.items() if k != 'provenance'}))
        item = deepcopy(observations[0])
        item['provenance'] = sorted({digest(p): p for r in values for p in r['provenance']}.values(),
                                    key=lambda p: (p['tileId'], p['query'], p['pageNum']))
        item['sourceTags'] = sorted({t for r in values for t in r['sourceTags']})
        item['warnings'] = sorted({w for r in values for w in r['warnings']})
        item['observations'] = [{k: r[k] for k in ('name', 'address', 'location', 'navigationLocation',
                                                   'confirmedEntrances', 'parentUid', 'sourceTags')}
                                for r in observations]
        is_outside = not any(counted(r['location']) for r in values)
        if is_outside:
            # The record keeps where it is and what it was: a nearby facility
            # outside the counting region is evidence for the region, not noise.
            outside.append({'sourceUid': item['sourceUid'], 'category': item['category'],
                            'location': item['location'],
                            'reason': 'outside_counting_region' if within is not None else 'outside_search_window',
                            'provenance': item['provenance']})
            if nearby is None:
                continue
        names = ' / '.join(sorted({r['name'] for r in values}))
        category, status, evidence = classify(names, item['sourceTags'])
        conflicts = []
        if len({tuple(r['location'].values()) for r in values}) > 1:
            conflicts.append('uid_location_conflict')
        for field, reason in (('name', 'uid_name_conflict'), ('address', 'uid_address_conflict'),
                              ('parentUid', 'uid_parent_conflict')):
            if len({canonical(r[field]) for r in values if r[field]}) > 1:
                conflicts.append(reason)
        verdicts = {r['classificationStatus'] for r in values}
        if 'excluded' in verdicts and len(verdicts) > 1:
            conflicts.append('uid_classification_conflict')
        if conflicts:
            category, status = None, 'needs_review'
        if status == 'accepted' and category not in request.categories:
            category, status, evidence = None, 'excluded', ['category_not_requested']
        item.update(category=category, classificationStatus=status,
                    classificationEvidence=sorted(set(evidence + conflicts)), conflicts=conflicts)
        if is_outside:
            if status == 'accepted':
                nearby.append({**item, 'countingRegion': 'outside'})
            continue
        {'accepted': accepted, 'needs_review': review, 'excluded': excluded}[status].append(item)
    mark_duplicates(accepted + review, projection)
    return accepted, review, excluded, outside, len(records)-len(by_uid)


def canonical(value):
    return re.sub(r'[\s()（）,，.。·]+', '', unicodedata.normalize('NFKC', value).casefold())


def mark_duplicates(items, projection):
    buckets = defaultdict(list)
    for item in items:
        if item['address']:
            buckets[(canonical(item['name']), canonical(item['address']))].append(item)
    for bucket in buckets.values():
        parents = list(range(len(bucket)))
        def root(i):
            while parents[i] != i:
                i = parents[i]
            return i
        points = [projection.to_local(tuple(item['location'].values())) for item in bucket]
        for i in range(len(bucket)):
            for j in range(i):
                if math.dist(points[i], points[j]) <= RULES['possibleDuplicateMeters']:
                    parents[root(i)] = root(j)
        groups = defaultdict(list)
        for i, item in enumerate(bucket):
            groups[root(i)].append(item)
        for group in groups.values():
            if len(group) > 1:
                identity = 'possible:' + digest(sorted(p['id'] for p in group))[:16]
                for item in group:
                    item['possibleDuplicateGroup'] = identity
