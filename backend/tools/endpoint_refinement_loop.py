"""E8.2.1 closed-loop refinement: small batches, one evidence version per rebuild.

After the 16-direction initialization four kinds of action share the remaining
budget: a new direction between two boundary nodes, a scan ray inside a local
repair patch, bisection of a mixed Delaunay edge, and exploration inside large
unverified faces or just outside the estimate. A batch ends once it has spent
BATCH_ATTEMPTS attempts; then everything is rebuilt from all evidence, so the
published boundary, its patches and the solver-unresolved region always come
from one evidence version.

The budget is a ceiling, not a target: the loop stops when no action is worth a
query. Priorities are fixed tiers -- evidence conflicts, then unlocalized
boundary, then wide angular gaps -- rather than an information-gain model.
Geometry only ever uses actual route endpoints; requested angles steer queries.

Rebuilding and planning are pure CPU over this task's own state, so they run on a
worker thread: the event loop stays free for status polls and other tasks while
the loop awaits them, and nothing else touches the state meanwhile.
"""
import asyncio
import math

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPoint, Point, box
from shapely.ops import triangulate, unary_union

from life_circle.field import multipolygon
from tools.endpoint_boundary_band import TAU, connect_estimate, nodes_from_rows, probe_sides
from tools.endpoint_boundary_surface import add_origin_condition
from tools.endpoint_geometry import (OVERLAY_GRID_M, business_geometry, polygon_difference,
                                     polygon_intersection, polygon_union)
from tools.endpoint_multicross_boundary import discover_patches

VERSION = 'local-multicross-e82.1'
BATCH_ATTEMPTS = 8
EXPLORE_SHARE = .2
COARSE_CHORD_M = 120
MAX_DIRECTIONS = 256
EDGE_ROUNDS = 4
DIRECTION_ROUNDS = 8
# Tiers: a conflict outranks an unlocalized boundary, which outranks a wide gap.
CONFLICT, UNLOCALIZED, GAP, SUPPORT = 3, 2, 1, 5
CONFLICT_REACH_M = 150
EXPLORE_MIN_FACE_M2 = 2500
EXPLORE_OUTER_SCALES = (1.15, 1.3, 1.45)
EXPLORE_OUTER_POINTS = 96
GOLDEN = (math.sqrt(5) - 1) / 2


def _key(xy):
    return tuple(round(v, 3) for v in xy)


# -- one triangulation per evidence version -------------------------------------
#
# Same semantics as endpoint_multicross_boundary.patch_triangles / mixed_edges /
# connect_patch / close_patch_evidence, but the Delaunay mesh is built once per
# rebuild and triangles are filtered against the patch in one vectorized pass.

class Mesh:
    def __init__(self, records):
        lookup = {tuple(r['xy']): r for r in records}
        polygons = list(triangulate(MultiPoint(list(lookup)))) if len(lookup) >= 3 else []
        self.triangles = np.array(polygons, dtype=object)
        # GEOS returns the input coordinates unchanged, so they key straight back to records.
        corners = shapely.get_coordinates(self.triangles).reshape(-1, 4, 2)[:, :3] if polygons else []
        self.vertices = [[lookup[(float(x), float(y))] for x, y in triangle] for triangle in corners]
        # A face is its corners: the same three records always span the same triangle.
        self.keys = [tuple(r['id'] for r in triangle) for triangle in self.vertices]

    def overlapping(self, region, *, clip=True, subset=None):
        """Triangles overlapping ``region`` with positive area: (index, overlap, whole).

        A triangle strictly inside the region is ``whole``: it is its own overlap and
        carries none. Only the ones crossing the region's boundary are intersected.
        ``subset`` restricts the test to those triangle indices.
        """
        if not len(self.triangles) or region.is_empty:
            return []
        shapely.prepare(region)
        candidates = np.arange(len(self.triangles)) if subset is None else np.asarray(subset, dtype=int)
        if not len(candidates):
            return []
        index = candidates[shapely.intersects(region, self.triangles[candidates])]
        if not len(index):
            return []
        inside = shapely.contains_properly(region, self.triangles[index])
        found = [(int(i), None, True) for i in index[inside]]
        edge = index[~inside]
        if len(edge):
            clipped = shapely.intersection(self.triangles[edge], region, grid_size=OVERLAY_GRID_M)
            found += [(int(i), multipolygon(c) if clip else None, False)
                      for i, c in zip(edge, clipped) if shapely.area(c) > 0]
        return sorted(found, key=lambda item: item[0])


def _coverage(faces):
    """Whole Delaunay faces share exact edges, so they merge as a coverage in one
    linear pass instead of a full overlay; the overlay that follows still snaps
    everything to the overlay grid.

    GEOS does not check its input: overlapping faces (near-degenerate slivers can
    look so) either raise or come back as an invalid result. Then, or whenever the
    merged area is not the faces' total, the faces go to the overlay as they are.
    """
    if len(faces) < 2:
        return list(faces)
    try:
        merged = shapely.coverage_union_all(faces)
    except shapely.errors.GEOSException:
        return list(faces)
    total = float(shapely.area(faces).sum())
    if not merged.is_valid or abs(merged.area - total) > 1e-6 * max(1.0, total):
        return list(faces)
    return [merged]


def _covered(region, records):
    """``region.covers`` for many records at once: a point is covered iff it intersects."""
    if not records:
        return np.zeros(0, dtype=bool)
    xy = np.array([r['xy'] for r in records], dtype=float)
    return shapely.intersects_xy(region, xy[:, 0], xy[:, 1])


def _long_mixed_edge(vertices, target):
    return any((a['duration'] <= 900) != (b['duration'] <= 900) and math.dist(a['xy'], b['xy']) > target
               for a, b in zip(vertices, vertices[1:] + vertices[:1]))


def mesh_mixed_edges(mesh, patch, target):
    edges = {}
    # Only a face with a long mixed edge can contribute: test the patch on those alone.
    wanted = [i for i, vertices in enumerate(mesh.vertices) if _long_mixed_edge(vertices, target)]
    for i, _, _ in mesh.overlapping(patch, clip=False, subset=wanted):
        vertices = mesh.vertices[i]
        for j, a in enumerate(vertices):
            b = vertices[(j + 1) % 3]
            width = math.dist(a['xy'], b['xy'])
            if (a['duration'] <= 900) != (b['duration'] <= 900) and width > target:
                edges[tuple(sorted((a['id'], b['id'])))] = (width, a, b)
    return edges


def _mixed_pieces(mesh, i, domain, target):
    """A mixed face's reachable estimate, and its unreachable rest once localized."""
    vertices = mesh.vertices[i]
    crossings, widths = [], []
    for j, a in enumerate(vertices):
        b = vertices[(j + 1) % 3]
        if (a['duration'] <= 900) != (b['duration'] <= 900):
            crossings.append([(x + y) / 2 for x, y in zip(a['xy'], b['xy'])])
            widths.append(math.dist(a['xy'], b['xy']))
    estimate = MultiPoint([p['xy'] for p in vertices if p['duration'] <= 900] + crossings).convex_hull
    reach = polygon_intersection(estimate, domain)
    if max(widths) > target:
        return reach, None
    return reach, polygon_intersection(polygon_difference(mesh.triangles[i], estimate), domain)


def mesh_connect_patch(mesh, domain, blocked=(), *, target=25, faces=None):
    """No time interpolation. Unlocalized mixed triangles remain unknown.

    ``faces`` caches each mixed face that lies wholly inside the patch: there its
    pieces do not depend on the patch, and its corners never change, so a later
    rebuild reuses them instead of overlaying the face again.
    """
    inside, outside, candidates = [], [], []
    whole_in, whole_out = [], []
    overlap = mesh.overlapping(domain)
    blocked_hits = set()
    if blocked and overlap:
        tree = shapely.STRtree([Point(p) for p in blocked])
        subset = np.array([mesh.triangles[i] for i, _, _ in overlap], dtype=object)
        pairs = tree.query(subset, predicate='covers')
        blocked_hits = {overlap[k][0] for k in pairs[0]}
    for i, clipped, whole in overlap:
        if i in blocked_hits:
            continue
        labels = [p['duration'] <= 900 for p in mesh.vertices[i]]
        if all(labels):
            if whole:
                whole_in.append(i)
            else:
                inside.append(clipped)
                candidates.append(clipped)
            continue
        if not any(labels):
            if whole:
                whole_out.append(i)
            else:
                outside.append(clipped)
            continue
        cached = faces is not None and whole
        pieces = faces.get(mesh.keys[i]) if cached else None
        if pieces is None:
            pieces = _mixed_pieces(mesh, i, domain, target)
            if cached:
                faces[mesh.keys[i]] = pieces
        reach, rest = pieces
        candidates.append(reach)
        if rest is not None:
            inside.append(reach)
            outside.append(rest)
    merged_in = _coverage(mesh.triangles[whole_in])
    reachable = polygon_union([*merged_in, *inside])
    unreachable = polygon_union([*_coverage(mesh.triangles[whole_out]), *outside])
    return dict(reachable=reachable, unreachable=unreachable,
                unknown=polygon_difference(domain, polygon_union([reachable, unreachable])),
                candidate=polygon_union([*merged_in, *candidates]))


def mesh_close_patch_evidence(mesh, records, patch, base, blocked, *, target=25, domain=None, faces=None):
    """Revoke stale faces around observed negatives at the repair seam."""
    original_area = patch.area
    negatives = [r for r in records if r['duration'] > 900]
    ids = {r['id'] for r, covered in zip(negatives, _covered(base, negatives)) if covered}
    incident = [i for i, vertices in enumerate(mesh.vertices) if any(r['id'] in ids for r in vertices)]
    patch = unary_union([patch, *_coverage(mesh.triangles[incident])])
    if domain is not None:
        patch = patch.intersection(domain)
    connected = mesh_connect_patch(mesh, patch, blocked, target=target, faces=faces)
    remainder = polygon_difference(base, patch)
    estimate = polygon_union([remainder, connected['reachable']])
    candidate = polygon_union([remainder, connected['candidate']])
    conflicts = [r['id'] for r, covered in zip(negatives, _covered(estimate, negatives)) if covered]
    return dict(**connected, patch=patch, estimate=estimate, combined_candidate=candidate,
                conflicts=conflicts, expanded_area_m2=patch.area - original_area)


class LoopState:
    def __init__(self, session, rows, *, target, radial_step, coarse_chord, explore_share):
        self.session, self.rows = session, rows
        self.target, self.radial_step, self.coarse_chord = target, radial_step, coarse_chord
        self.start_calls = session.scheduler.stats.requests
        self.initial_remaining = max(0, session.scheduler.remaining)
        self.explore_reserve = int(explore_share * self.initial_remaining)
        self.explore_spent = 0
        self.patch = polygon_union([])
        self.spec_keys, self.scan_queue, self.scanned = set(), [], set()
        self.attempted_edges, self.attempted_angles = set(), []
        self.failed_directions, self.explored = [], set()
        self.handled_points = set()
        self.outer_index, self.pending_outer = 0, None
        self.calls = dict(direction=0, scan=0, edge=0, explore=0)
        self.counts = dict(batches=0, directions_added=0, directions_failed=0, repeated_endpoints=0,
                           scan_rays=0, edge_brackets=0, explore_probes=0, explore_contradictions=0,
                           conflict_patches=0, outside_positive_patches=0)
        # Pieces of mixed faces wholly inside the patch, kept across rebuilds.
        self.faces = {}
        self.version = None
        self.stop_reason = None

    @property
    def scheduler(self):
        return self.session.scheduler

    @property
    def used(self):
        return self.scheduler.stats.requests - self.start_calls

    def stopped(self):
        return self.scheduler._stopped() or self.scheduler.remaining <= 0

    def reserve_left(self):
        return max(0, self.explore_reserve - self.explore_spent)

    def origin(self):
        anchor = add_origin_condition(self.session)
        return anchor['xy'] if anchor else (0, 0)


# -- rebuild: one evidence version --------------------------------------------

def _add_scan_spec(state, angle, gap, start, end, reason):
    key = (round(angle % TAU, 6), round(gap, 6), round(start, 1), round(end, 1))
    if key in state.spec_keys:
        return
    state.spec_keys.add(key)
    count = max(1, math.ceil(gap * end / 75))
    for i in range(count + 1):
        state.scan_queue.append(dict(angle=(angle + gap * i / count) % TAU, start=start, end=end,
                                     reason=reason, key=(round((angle + gap * i / count) % TAU, 6),
                                                         round(start, 1), round(end, 1))))


def _point_patch(state, xy, reason):
    """A contradiction seen at one point opens a local window around it."""
    origin = state.origin()
    x, y = xy
    r = math.dist(xy, origin)
    angle = math.atan2(y - origin[1], x - origin[0]) % TAU
    gap = min(.5, 150 / max(r, 1))
    domain = state.session.domain
    state.patch = polygon_union([state.patch, box(x - 100, y - 100, x + 100, y + 100).intersection(domain)])
    _add_scan_spec(state, angle - gap / 2, gap, max(0, r - 150), r + 150, reason)


def rebuild(state):
    session = state.session
    domain = session.domain
    origin = state.origin()
    specs, patch, base = discover_patches(state.rows, origin, domain, session._conflicts, session.records)
    for spec in specs:
        _add_scan_spec(state, spec['angle'], spec['gap'], spec['start'], spec['end'], 'patch')
    state.patch = polygon_union([state.patch, patch])
    records = session.records
    mesh = Mesh(records)
    version = dict(origin=origin, base=base, mesh=mesh, estimate=None, connected=None,
                   conflicts=[], conflict_xy=[])
    if base['candidate'] is not None:
        blocked = list(session._conflicts) + [session.projection.to_local(e['destination'])
                                               for e in session.log if not e['accepted']]
        by_id = {r['id']: r for r in records}
        # A contradiction opens a new patch; the version is then reassembled from the
        # same evidence so boundary, patches and unresolved region never disagree.
        # Each point opens at most once, so this terminates.
        while True:
            connected = mesh_close_patch_evidence(mesh, records, state.patch, base['candidate'], blocked,
                                                  target=state.target, domain=domain, faces=state.faces)
            state.patch = connected['patch'].intersection(domain)
            estimate = connected['estimate'].intersection(domain)
            if not _open_contradictions(state, records, by_id, connected, estimate):
                break
        version.update(estimate=estimate, connected=connected, conflicts=connected['conflicts'],
                       conflict_xy=[by_id[rid]['xy'] for rid in connected['conflicts'] if rid in by_id])
    state.version = version
    return version


def _open_contradictions(state, records, by_id, connected, estimate):
    """Negatives the estimate covers and positives far outside it get a local window."""
    opened = 0
    for rid in connected['conflicts']:
        record = by_id.get(rid)
        if record and ('conflict', rid) not in state.handled_points:
            state.handled_points.add(('conflict', rid))
            state.counts['conflict_patches'] += 1
            _point_patch(state, record['xy'], 'conflict')
            opened += 1
    positives = [r for r in records if r['duration'] <= 900
                 and r.get('source_kind') != 'model_origin_condition'
                 and ('outside', r['id']) not in state.handled_points]
    if positives:
        xs = np.array([r['xy'][0] for r in positives])
        ys = np.array([r['xy'][1] for r in positives])
        grown = estimate.buffer(state.target)
        # Inside a patch an unplaced positive is pending edge work, not a contradiction.
        placed = shapely.intersects_xy(grown, xs, ys) | shapely.intersects_xy(state.patch, xs, ys)
        positives = [r for r, ok in zip(positives, placed) if not ok]
    for record in positives:
        state.handled_points.add(('outside', record['id']))
        state.counts['outside_positive_patches'] += 1
        _point_patch(state, record['xy'], 'outside_positive')
        opened += 1
    return opened


# -- actions ------------------------------------------------------------------

def _near_conflict(state, xy):
    points = state.version['conflict_xy'] if state.version else []
    return any(math.dist(p, xy) <= CONFLICT_REACH_M for p in points)


def _direction_actions(state):
    version = state.version
    origin, base = version['origin'], version['base']
    nodes = nodes_from_rows(state.rows, origin, state.session.domain, state.session._conflicts)
    if len(state.rows) + len(state.failed_directions) >= MAX_DIRECTIONS:
        return []
    actions = []
    uncommitted = [r for r in state.failed_directions]
    for i, a in enumerate(nodes):
        b = nodes[(i + 1) % len(nodes)] if len(nodes) > 1 else None
        if b is None:
            break
        gap = (b['angle'] - a['angle']) % TAU
        if gap <= 1e-6:
            continue
        angle = (a['angle'] + gap / 2) % TAU
        if any(abs((angle - old + math.pi) % TAU - math.pi) < gap / 4 for old in state.attempted_angles):
            continue
        chord = math.dist(a['xy'], b['xy'])
        ra, rb = math.dist(a['xy'], origin), math.dist(b['xy'], origin)
        hint = (ra + rb) / 2
        probe = (origin[0] + hint * math.cos(angle), origin[1] + hint * math.sin(angle))
        if state.patch.covers(Point(probe)):
            continue
        score = 0.0
        if base['candidate'] is None or gap >= math.pi / 2 - 1e-9:
            score = SUPPORT + gap
        unresolved = (any(n['width_m'] > state.target or n['bracket'].get('suspected_jump') for n in (a, b))
                      or any(((r['angle'] - a['angle']) % TAU) < gap for r in uncommitted))
        if unresolved:
            score = max(score, UNLOCALIZED + min(1, max(a['width_m'], b['width_m']) / 400))
        if chord > state.coarse_chord:
            score = max(score, GAP + min(1, (chord - state.coarse_chord) / state.coarse_chord))
        if _near_conflict(state, probe):
            score = max(score, CONFLICT)
        if score > 0:
            actions.append((score, chord, 'direction', dict(angle=angle, hint=hint, step=max(50, abs(ra - rb) / 2 + 50))))
    if len(nodes) < 3 and not actions:
        # Too little support for a ring: fill the widest requested gaps.
        taken = sorted(state.attempted_angles + [r['angle'] for r in state.rows])
        if taken:
            gaps = [((taken[(i + 1) % len(taken)] - t) % TAU or TAU, t) for i, t in enumerate(taken)]
            gap, start = max(gaps)
            actions.append((SUPPORT + gap, gap, 'direction', dict(angle=(start + gap / 2) % TAU, hint=600, step=150)))
    return actions


def _patch_actions(state):
    actions = []
    # Scans discover structure, edges resolve it. Like the legacy 50/50 split, the two
    # alternate: scans lead while they have not out-spent edge bisection by a batch.
    scans_lead = state.calls['scan'] <= state.calls['edge'] + BATCH_ATTEMPTS
    origin = state.version['origin']
    for ray in state.scan_queue:
        if ray['key'] in state.scanned:
            continue
        mid = (ray['start'] + ray['end']) / 2
        xy = (origin[0] + mid * math.cos(ray['angle']), origin[1] + mid * math.sin(ray['angle']))
        # Only a covered negative is a conflict; a positive seen outside is a lead.
        if ray['reason'] == 'conflict' or _near_conflict(state, xy):
            score = CONFLICT
        else:
            score = UNLOCALIZED + (.995 if scans_lead else -.5)
        actions.append((score, -ray['angle'], 'scan', ray))
    for key, (width, a, b) in mesh_mixed_edges(state.version['mesh'], state.patch, state.target).items():
        if key in state.attempted_edges:
            continue
        mid = [(x + y) / 2 for x, y in zip(a['xy'], b['xy'])]
        score = CONFLICT if _near_conflict(state, mid) else UNLOCALIZED
        actions.append((score + min(.99, width / 1000), width, 'edge', dict(key=key, a=a, b=b)))
    return actions


def _explore_candidates(state):
    """Interior faces nobody checked, then points just outside the estimate."""
    estimate = state.version['estimate']
    if estimate is None or estimate.is_empty:
        return []
    mesh = state.version['mesh']
    candidates = []
    if len(mesh.triangles):
        # Faces filled only because all three corners are reachable: nobody checked inside.
        areas = shapely.area(mesh.triangles)
        reachable = np.array([all(v['duration'] <= 900 for v in vertices) for vertices in mesh.vertices])
        chosen = np.nonzero((areas >= EXPLORE_MIN_FACE_M2) & reachable)[0]
        centroids = shapely.get_coordinates(shapely.centroid(mesh.triangles[chosen]))
        covered = shapely.intersects_xy(estimate, centroids[:, 0], centroids[:, 1])
        for i, (x, y), inside in zip(chosen, centroids.tolist(), covered):
            key = ('face', _key((x, y)))
            if inside and key not in state.explored:
                candidates.append((float(areas[i]), key, (x, y)))
    candidates.sort(reverse=True)
    interior = [(key, xy) for _, key, xy in candidates[:1]]
    origin = state.version['origin']
    domain = state.session.domain
    # The next exterior point is held until it is actually probed.
    while state.pending_outer is None and state.outer_index < EXPLORE_OUTER_POINTS * len(EXPLORE_OUTER_SCALES):
        index = state.outer_index
        state.outer_index += 1
        angle = (index * GOLDEN % 1) * TAU
        scale = EXPLORE_OUTER_SCALES[index % len(EXPLORE_OUTER_SCALES)]
        ray = LineString([origin, (origin[0] + 4000 * math.cos(angle), origin[1] + 4000 * math.sin(angle))])
        crossing = ray.intersection(estimate.boundary)
        if crossing.is_empty:
            continue
        points = [crossing] if crossing.geom_type == 'Point' else list(getattr(crossing, 'geoms', []))
        reach = max((math.dist(origin, (p.x, p.y)) for p in points if p.geom_type == 'Point'), default=None)
        if reach is None:
            continue
        xy = (origin[0] + reach * scale * math.cos(angle), origin[1] + reach * scale * math.sin(angle))
        key = ('outer', _key(xy))
        if key not in state.explored and domain.covers(Point(xy)):
            state.pending_outer = (key, xy)
    exterior = [state.pending_outer] if state.pending_outer is not None else []
    # Alternate: interior when available on even probes, exterior otherwise.
    order = interior + exterior if state.counts['explore_probes'] % 2 == 0 else exterior + interior
    return order


async def search_near(state, angle, hint, step):
    """Two-sided bracket search from a neighbour-informed radius.

    Returns a direction row; only a localized bracket is committed as a node.
    A second repeat of an already known actual endpoint abandons the direction:
    it costs budget but adds no evidence.
    """
    session, target = state.session, state.target
    origin = state.version['origin']
    ux, uy = math.cos(angle), math.sin(angle)
    extent = state.scheduler.request.extent
    cap = max(0, extent - .1 - max(abs(origin[0]), abs(origin[1]))) / max(abs(ux), abs(uy))
    row = dict(angle=angle, status='unknown', reason=None, bracket=None, committed=False, source='densify')
    known = {r['id'] for r in session.records}
    repeats = 0

    async def measure(radius):
        nonlocal repeats
        record, error = await session.measure((origin[0] + radius * ux, origin[1] + radius * uy), 'densify_search')
        if record is not None:
            if record['id'] in known:
                repeats += 1
                state.counts['repeated_endpoints'] += 1
            else:
                repeats = 0
                known.add(record['id'])
        return record, error

    radius = min(max(hint, 25), cap)
    record, error = await measure(radius)
    if record is None:
        row['reason'] = error
        return row
    inside = outside = None
    if record['duration'] <= 900:
        inside = record
    else:
        outside = record
    while inside is None or outside is None:
        if state.stopped():
            row['reason'] = state.scheduler.stop_reason or 'budget'
            return row
        if repeats >= 2:
            row['reason'] = 'repeated_actual_endpoint'
            return row
        if outside is None:
            if radius >= cap - 1e-7:
                row.update(status='truncated', reason='range_limit')
                return row
            radius = min(cap, radius + step)
            record, error = await measure(radius)
            if record is None:
                row['reason'] = error
                return row
            if record['duration'] <= 900:
                inside = record
            else:
                outside = record
        else:
            radius -= step
            if radius <= 25:
                inside = add_origin_condition(session)
                if inside is None:
                    row['reason'] = 'missing_inside_anchor'
                    return row
                break
            record, error = await measure(radius)
            if record is None:
                row['reason'] = error
                return row
            if record['duration'] <= 900:
                inside = record
            else:
                outside = record
        step *= 2
    if not session.domain.covers(Point(outside['xy'])) or not session.domain.covers(Point(inside['xy'])):
        row['reason'] = 'actual_endpoint_outside_domain'
        return row
    bracket = await session.refine(inside, outside, target=target, max_rounds=DIRECTION_ROUNDS)
    if bracket['reason'] == 'offset_stagnation':
        bracket, row['sideProbes'] = await probe_sides(session, bracket, target=target)
    row.update(status=bracket['status'], reason=bracket['reason'], bracket=bracket,
               committed=bracket['status'] == 'localized')
    return row


async def _run_action(state, kind, spec):
    session = state.session
    before = state.scheduler.stats.requests
    if kind == 'direction':
        state.attempted_angles.append(spec['angle'])
        row = await search_near(state, spec['angle'], spec['hint'], spec['step'])
        if row['committed']:
            state.rows.append(row)
            state.counts['directions_added'] += 1
        else:
            state.failed_directions.append(row)
            state.counts['directions_failed'] += 1
    elif kind == 'scan':
        state.scanned.add(spec['key'])
        state.counts['scan_rays'] += 1
        ox, oy = state.version['origin']
        ux, uy = math.cos(spec['angle']), math.sin(spec['angle'])
        count = max(1, math.ceil((spec['end'] - spec['start']) / state.radial_step))
        for i in range(count + 1):
            if state.stopped():
                break
            radius = spec['start'] + (spec['end'] - spec['start']) * i / count
            if radius < 1:
                continue
            # Samples only: flips become mixed edges and are bisected as their own actions.
            await session.measure((ox + radius * ux, oy + radius * uy), 'local_radial_scan')
    elif kind == 'edge':
        state.attempted_edges.add(spec['key'])
        state.counts['edge_brackets'] += 1
        await session.refine(spec['a'], spec['b'], target=state.target, max_rounds=EDGE_ROUNDS)
    elif kind == 'explore':
        key, xy = spec
        state.explored.add(key)
        if state.pending_outer is not None and state.pending_outer[0] == key:
            state.pending_outer = None
        state.counts['explore_probes'] += 1
        record, _ = await session.measure(xy, 'exploration')
        estimate = state.version['estimate']
        if record is not None and estimate is not None:
            inside = estimate.covers(Point(record['xy']))
            if inside != (record['duration'] <= 900):
                state.counts['explore_contradictions'] += 1
    spent = state.scheduler.stats.requests - before
    state.calls[kind] += spent
    if kind == 'explore':
        state.explore_spent += spent
    return spent


def _plan(state):
    """Every action the current evidence version proposes, best first."""
    actions = sorted(_direction_actions(state) + _patch_actions(state),
                     key=lambda a: (a[0], a[1]), reverse=True)
    explore = _explore_candidates(state) if state.reserve_left() > 0 else []
    return actions, explore


async def refinement_loop(session, rows, *, target=25, radial_step=50, coarse_chord=COARSE_CHORD_M,
                          explore_share=EXPLORE_SHARE, token=None):
    state = LoopState(session, rows, target=target, radial_step=radial_step,
                      coarse_chord=coarse_chord, explore_share=explore_share)
    await asyncio.to_thread(rebuild, state)
    while True:
        if token is not None and token.cancelled:
            state.stop_reason = 'cancelled'
            break
        if state.stopped():
            state.stop_reason = state.scheduler.stop_reason or 'budget'
            break
        actions, explore = await asyncio.to_thread(_plan, state)
        if not explore:
            # Nothing left to explore: its reserve returns to the other actions.
            state.explore_reserve = state.explore_spent
        if not actions and not explore:
            state.stop_reason = 'no_ambiguity_left'
            break
        batch_start = state.scheduler.stats.requests
        progress = state.used / max(1, state.initial_remaining)
        only_reserve_left = state.scheduler.remaining <= state.reserve_left()
        # Exploration keeps pace with overall progress instead of waiting until the end.
        if explore and (not actions or only_reserve_left
                        or state.explore_spent < state.explore_reserve * progress + 1):
            await _run_action(state, 'explore', explore[0])
        for _, _, kind, spec in actions:
            if state.stopped() or state.scheduler.stats.requests - batch_start >= BATCH_ATTEMPTS:
                break
            # Other actions never eat into exploration's unspent reserve.
            if state.scheduler.remaining <= state.reserve_left():
                break
            await _run_action(state, kind, spec)
        state.counts['batches'] += 1
        if state.scheduler.stats.requests == batch_start:
            # Every candidate was cached or rejected before a send: nothing more to learn.
            state.stop_reason = 'no_ambiguity_left'
            await asyncio.to_thread(rebuild, state)
            break
        await asyncio.to_thread(rebuild, state)
    return state


# -- publication --------------------------------------------------------------

def carve_conflicts(state, estimate):
    """Remove a disk around every negative the estimate still covers.

    Radius min(50, 0.49 d) with d the distance to the nearest reachable evidence:
    no positive is ever removed, and the negative ends strictly outside.
    """
    records = state.session.records
    positives = [r for r in records if r['duration'] <= 900]
    disks, carved = [], []
    for record in records:
        if record['duration'] <= 900 or not estimate.covers(Point(record['xy'])):
            continue
        nearest = min((math.dist(record['xy'], p['xy']) for p in positives), default=100)
        radius = min(50, .49 * nearest)
        if radius <= 0:
            continue
        disks.append(Point(record['xy']).buffer(radius, quad_segs=16))
        carved.append(dict(id=record['id'], radiusM=radius))
    if not disks:
        return estimate, polygon_union([]), carved
    disk = polygon_union(disks).intersection(state.session.domain)
    return polygon_difference(estimate, disk), disk, carved


def _ring_assembly(version) -> dict:
    """环面合没合起来，没合起来是为什么。

    ``connect_estimate`` 早就把这句话算出来了 —— 拒绝时的 ``reason`` 与 ``max_gap_radians``
    —— 但一次没有几何的运行只把 ``budget`` 留下来：读者要自己翻源码才知道真正挡住它的是
    什么（已定的方向之间隔着多大一个角隙）。这里只搬运已经算出来的值，不改判定。
    """
    base = (version or {}).get('base') or {}
    gap = base.get('max_gap_radians')
    return dict(assembled=base.get('candidate') is not None,
                reason=base.get('reason') or None,
                maxGapRadians=float(gap) if isinstance(gap, (int, float))
                and math.isfinite(float(gap)) else None,
                localizedNodes=len(base.get('nodes') or []))


def publish(state, token=None):
    """Assemble the published extension from the last evidence version."""
    session = state.session
    domain = session.domain
    version = state.version
    projection = session.projection
    cancelled = bool(token is not None and token.cancelled)
    loop_meta = dict(version=VERSION, stopReason=state.stop_reason, calls=dict(state.calls),
                     counts=dict(state.counts), exploreReserve=state.explore_reserve,
                     exploreSpent=state.explore_spent, initialRemaining=state.initial_remaining,
                     parameters=dict(batchAttempts=BATCH_ATTEMPTS, exploreShare=EXPLORE_SHARE,
                                     coarseChordM=state.coarse_chord, maxDirections=MAX_DIRECTIONS,
                                     targetM=state.target, radialStepM=state.radial_step))
    initial_unfinished = sum(r.get('status') != 'localized' for r in state.rows if r.get('source') != 'densify')
    pending_rays = sum(ray['key'] not in state.scanned for ray in state.scan_queue)
    if version is None or version['estimate'] is None:
        loop_meta.update(carvedAreaM2=0, carvedNegatives=[], unresolvedInsideM2=0,
                         unresolvedOutsideM2=domain.area)
        # 没有环面时这两个数此前写死为 0：它们读起来像"网格里没有未决边"，而真实情况是
        # "根本没量过" —— 这两个数只在补丁窗口内量，而这里连窗口都没有，量出来只会是 0。
        # 所以它们是 null，不是 0；环到底为什么没合起来，写在 ringAssembly 里。
        return dict(geometry=None, candidateGeometry=None, unknownRegion=None,
                    quality='experimental_insufficient_support', uncertaintyBand=None,
                    uncertainty=dict(guaranteedCoverage=False, closedEstimate=False, segments=[],
                                     reason='insufficient_support',
                                     meaning='solver-unresolved region; not an error bound'),
                    completion=dict(scope='closed_loop_local_evidence', resolutionReached=False,
                                    budgetExhausted=session.scheduler.remaining <= 0,
                                    unresolvedEdges=None, pendingUnattemptedEdges=None,
                                    pendingScanRays=pending_rays,
                                    initialUnfinishedDirections=initial_unfinished,
                                    runComplete=not cancelled, reason=state.stop_reason),
                    ringAssembly=_ring_assembly(version),
                    localRepair=dict(patches=len(state.spec_keys), calls=state.used, scanRays=[],
                                     edgeBrackets=[], physicalBarrierVerified=False),
                    refinementLoop=loop_meta)
    connected = version['connected']
    # A mixed face that was not bisected to the target is still published at its
    # midpoint estimate (reachable corners plus crossing midpoints), exactly as the
    # star connects bracket midpoints, and stays inside the solver-unresolved
    # region. Dropping the whole face would bias the boundary inward.
    published = connected['combined_candidate'].intersection(domain)
    estimate, carved_region, carved = carve_conflicts(state, published)
    still = [r['id'] for r in session.records
             if r['duration'] > 900 and estimate.covers(Point(r['xy']))]
    # Unresolved boundary segments outside the patches: a strip, not an error bound.
    segments = connect_estimate(state.rows, version['origin'], domain, session._conflicts,
                                target=state.target, chord_target=state.coarse_chord,
                                witnesses=session.records)
    strips = [LineString([s['start_xy'], s['end_xy']]).buffer(max(s['width_m'] / 2, state.target))
              for s in segments['segments'] if s['reasons']]
    strip = polygon_difference(polygon_intersection(polygon_union(strips), domain), state.patch) if strips else polygon_union([])
    unresolved = polygon_intersection(polygon_union([connected['unknown'], strip, carved_region]), domain)
    unresolved_edges = mesh_mixed_edges(version['mesh'], state.patch, state.target)
    pending_edges = sum(k not in state.attempted_edges for k in unresolved_edges)
    resolution = (state.stop_reason == 'no_ambiguity_left' and not unresolved_edges and not still
                  and unresolved.area < 1e-6 and not cancelled)
    loop_meta.update(carvedAreaM2=carved_region.area, carvedNegatives=carved,
                     unresolvedInsideM2=unresolved.intersection(estimate).area,
                     unresolvedOutsideM2=unresolved.difference(estimate).area)
    geometry = None if still or estimate.is_empty else business_geometry(estimate, projection)
    return dict(
        geometry=geometry,
        candidateGeometry=business_geometry(published, projection),
        unknownRegion=business_geometry(unresolved, projection),
        quality='experimental_evidence_conflict' if still else 'experimental_closed_loop',
        uncertaintyBand=None,
        uncertainty=dict(guaranteedCoverage=False, closedEstimate=not still, segments=[],
                         reason='known_negative_inside_estimate' if still else 'solver_unresolved_region',
                         meaning='unknownRegion is the solver-unresolved region: patch faces not yet '
                                 'localized, unresolved boundary strips and carved disks. It may '
                                 'overlap geometry and is not an error bound.'),
        completion=dict(scope='closed_loop_local_evidence', resolutionReached=resolution,
                        budgetExhausted=session.scheduler.remaining <= 0,
                        unresolvedEdges=len(unresolved_edges), pendingUnattemptedEdges=pending_edges,
                        pendingScanRays=pending_rays, initialUnfinishedDirections=initial_unfinished,
                        runComplete=not cancelled and state.stop_reason not in ('deadline', 'upstream_failure'),
                        reason='resolution_reached' if resolution else state.stop_reason),
        ringAssembly=_ring_assembly(version),
        localRepair=dict(patches=len(state.spec_keys), calls=state.used, scanRays=[], edgeBrackets=[],
                         radialStepM=state.radial_step, targetM=state.target, physicalBarrierVerified=False,
                         negativeConflicts=still, connectionAssumption='homogeneous observed vertices; '
                         'exploration probes large unverified faces'),
        refinementLoop=loop_meta)
