"""§4.2 adaptive online planning: coarse blocks, rotation, subdivision, evidence.

The full-catalog fixed 4×4 plan in ``planner.py`` needs 1520 first-page requests and
cannot fit the former 60-attempt online budget. Checkups now allow up to 1200
attempts per round. This module plans the same
``around`` search adaptively instead: it starts from the query domain's envelope
split 2×2, keeps the blocks that intersect the domain, and spends its budget in a
rotation across categories so that no category is exhausted first.

A page is the unit of scheduling. ``fetch`` is injected and must perform exactly
one upstream attempt per call: pacing, the service's single in-flight slot and
the daily reservation belong to the quota pool that wraps it (§9), while retry
policy belongs here, because §4.2.5 makes paging, subdivision and retry share
one budget.

The run reports raw rows with the request that produced them. The caller
normalizes them and then clips with ``clip_to_domain``: a block is searched with
its circumscribed circle, which reaches past the domain, and only the domain's
part is a facility finding.

Incomplete evidence is never discarded and never rounded up. A truncated or
unstable block is subdivided and queried again with smaller circles when it can
still be subdivided; when it cannot — the finest block still saturated, the
budget gone, a query failed — the coverage says so and the run stays ``partial``.
Finishing a keyword search is not evidence that the real directory is complete,
so this module cannot express a completeness claim at all: the only value it can
report is ``unverified`` (§4.2).

Two ceilings, never one. What a run may spend on *new network calls* belongs to
the task bucket and the daily ledger, which refuse atomically at the moment of
dispatch; what it may spend on *local scheduling* — cached replays, subdivision,
retries — is bounded here, by :data:`PROCESSING_STEP_LIMIT`. Collapsing them was
the bug this module now avoids: a replay of a page the cache already holds costs
no allowance, so counting it against the paid budget stopped a second retrieval at
the very boundary the first one reached, and a run that had spent its allowance
threw away the pages the cache could still answer. A caller that names only
``budget`` keeps the single ceiling it has always been promised
(:func:`legacy_limits`); one that brings :class:`RunLimits` says the two apart.
"""
import asyncio
import copy
import math
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Protocol

import httpx

from app.cache import SharedBuildFailed
from app.place_protocol import RETRY_ERRORS, STOP_ERRORS, Pagination
from app.quota import QuotaError
from app.request_control import RequestStopped
from life_circle.coordinates import LocalProjection
from life_circle.models import CancelToken
from app import catalog

from .planner import RULES, sequence

FINEST_BLOCK_METERS = 250.0
MAX_PAGES = 8
MAX_PAGE_ATTEMPTS = 2

#: The local ceiling on one adaptive run: how many page requests it will schedule,
#: cached replays, subdivision and retries included. It is deliberately *not* the
#: network budget. Replaying a page the cache already holds costs no allowance, so
#: counting those replays against the paid allowance is what used to stop a second
#: retrieval at the very boundary the first one reached. This ceiling exists for the
#: other job — a provider that keeps answering, an anomalous pagination or a
#: subdivision loop still terminates in bounded time.
PROCESSING_STEP_LIMIT = 4096

#: Why a run stopped, kept apart on purpose. ``processing_limit_reached`` says the
#: local scheduling ceiling was met; ``network_budget_exhausted`` says new upstream
#: calls ran out — and a run may reach that point having delivered every cached page
#: it could. Reading either one as "the network allowance is gone" would be wrong.
PROCESSING_LIMIT_REACHED = 'processing_limit_reached'
NETWORK_BUDGET_EXHAUSTED = 'network_budget_exhausted'
#: The legacy single ceiling, still reported to callers that pass only ``budget``.
LEGACY_BUDGET_EXHAUSTED = 'budget_exhausted'

#: How one page request was answered. ``live`` dispatched this call upstream;
#: ``cache`` read a page that was already stored; ``shared`` awaited an identical
#: in-flight request some other call paid for; ``refused`` never got past a
#: scheduling check, so it cost nothing at all.
LIVE, CACHED, SHARED, REFUSED = 'live', 'cache', 'shared', 'refused'
# The provider's documented page size and ``total`` ceiling are enforced where
# they belong — the wire parameters in ``planner.parameters`` and the pagination
# rules in ``place_protocol.Pagination`` — rather than restated here.

# A density signal says the block's result set was cut short for reasons smaller
# circles can resolve, so the block is still worth subdividing (§4.2.4).
DENSITY_SIGNALS = frozenset({'possible_truncation', 'page_limit',
                             'pagination_anomaly', 'pagination_uncertain'})

# Throttling is the one refusal worth a bounded retry: the shared gate already
# cools down after a ``rate_limit`` (``analyses.RateGate.completed``), so a
# transient concurrency refusal recovers instead of ending the whole retrieval.
# A second refusal means the service is still throttling, so the run pauses and
# keeps what it obtained rather than paying for pages that would be refused too.
THROTTLING = frozenset({'rate_limit'})
#: Reasons this module retries once, inside the same budget.
RETRYABLE = RETRY_ERRORS | THROTTLING

# A refusal the scheduling layer will answer the same way for every remaining
# sequence. Stopping is the only correct response and every sequence keeps
# whatever it obtained. Throttling is excluded here and handled as a bounded
# retry above; ``place_protocol.STOP_ERRORS`` itself stays untouched, because
# the legacy ``/api/analyses`` place search and the command-line POI runtime
# still read it as "stop now".
FATAL = frozenset((STOP_ERRORS - THROTTLING)
                  | {'task_budget_exhausted', 'daily_budget_exhausted',
                     'deadline_reached', 'matrix_disabled'})
#: Refusals that mean "this application may not send more requests right now": the
#: task's own bucket is empty or the day's allowance is. A run that keeps its two
#: ceilings apart treats them as one page's outcome and carries on, because the
#: pages the cache can still answer cost nothing and must not be lost with them.
BUDGET_REFUSALS = frozenset({'task_budget_exhausted', 'daily_budget_exhausted'})

# Only for floating-point inversion at exact edges, as in ``normalize.inside``.
EDGE_TOLERANCE = 1e-6


class Fetch(Protocol):
    """One upstream attempt for one page; retries are this module's business."""

    async def __call__(self, sequence: dict, page: int) -> tuple[dict | None, str | None]: ...


@dataclass(frozen=True)
class PageResponse:
    """One page request's answer, and who paid for it.

    ``manner`` is what the accounting needs and what a bare payload cannot say: a
    cache hit and a dispatched call return exactly the same page. It is written by
    whichever layer actually resolved the request — the cache wrapper knows whether
    it read a stored page or awaited someone else's in-flight one — rather than
    inferred afterwards from a counter that moved.
    """

    payload: dict | None
    reason: str | None
    manner: str = LIVE

    def __iter__(self):
        """A page source may still answer with the plain ``(payload, reason)`` pair."""
        return iter((self.payload, self.reason))

    @property
    def dispatched(self) -> bool:
        return self.manner == LIVE


@dataclass(frozen=True)
class RunLimits:
    """The ceilings one adaptive run obeys, kept apart on purpose.

    ``processing_steps`` bounds local scheduling. ``drain_after_budget_refusal``
    decides what a spent allowance means for the run: with it, the refusal is one
    page's outcome and the run keeps going so that every page the cache can still
    answer is delivered; without it, a budget refusal ends the run — which is what
    a caller that names a single ``budget`` has always been promised.
    """

    processing_steps: int
    drain_after_budget_refusal: bool = False
    processing_stop: str = PROCESSING_LIMIT_REACHED
    network_stop: str = NETWORK_BUDGET_EXHAUSTED

    def __post_init__(self):
        if type(self.processing_steps) is not int or self.processing_steps <= 0:
            raise ValueError('processing_steps must be a positive integer')


def _shared_reason(cause: BaseException) -> str | None:
    """The reason an in-flight call this run did not dispatch ended in.

    ``None`` means the exception is not one this module names; the caller re-raises
    the original so unknown failures keep propagating exactly as they did before.
    """
    if isinstance(cause, RequestStopped):
        return cause.reason
    if isinstance(cause, QuotaError):
        return cause.code
    if isinstance(cause, httpx.TimeoutException):
        return 'timeout'
    if isinstance(cause, httpx.RequestError):
        return 'network_error'
    return None


def legacy_limits(budget: int) -> RunLimits:
    """The single ceiling ``OnlinePlanner(budget=…)`` has always meant.

    Every page the planner scheduled counted against it, cached or not, and a
    budget refusal ended the run. Kept as its own constructor so the old callers
    — the command-line runtime and the tests that pin its semantics — keep exactly
    the behaviour they were written against.
    """
    if type(budget) is not int or budget <= 0:
        raise ValueError('budget must be a positive integer')
    return RunLimits(processing_steps=budget, drain_after_budget_refusal=False,
                     processing_stop=LEGACY_BUDGET_EXHAUSTED)


@dataclass(frozen=True)
class QueryBlock:
    """One square query block; ``tile_id`` names its path in the subdivision."""
    tile_id: str
    x: float
    y: float
    edge: float

    def __post_init__(self):
        if (not math.isfinite(self.x) or not math.isfinite(self.y)
                or not math.isfinite(self.edge) or self.edge <= 0):
            raise ValueError('invalid_query_block')

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.x + self.edge, self.y + self.edge)

    @property
    def radius(self) -> int:
        """The circle that circumscribes the block, as ``planner.sequence`` sends it."""
        return math.ceil(self.edge / math.sqrt(2)) + 5

    def subdividable(self, finest: float = FINEST_BLOCK_METERS) -> bool:
        return self.edge / 2 >= finest

    def children(self, finest: float = FINEST_BLOCK_METERS) -> tuple:
        """The four blocks that tile this one, or none when this is the finest."""
        if not self.subdividable(finest):
            return ()
        half = self.edge / 2
        return tuple(QueryBlock(f'{self.tile_id}.{index}', self.x + (index % 2)*half,
                                self.y + (index // 2)*half, half) for index in range(4))


def _orientation(a, b, c):
    return (b[0]-a[0]) * (c[1]-a[1]) - (b[1]-a[1]) * (c[0]-a[0])


def _segment_distance(a, b, point):
    dx, dy = b[0]-a[0], b[1]-a[1]
    length2 = dx*dx + dy*dy
    if length2 == 0:
        return math.dist(a, point)
    t = max(0.0, min(1.0, ((point[0]-a[0])*dx + (point[1]-a[1])*dy) / length2))
    return math.dist((a[0] + t*dx, a[1] + t*dy), point)


def _near_segment(point, a, b):
    return _segment_distance(a, b, point) <= EDGE_TOLERANCE


def _crosses(p, q, r, s):
    """Whether two closed segments share a point."""
    o1, o2 = _orientation(p, q, r), _orientation(p, q, s)
    o3, o4 = _orientation(r, s, p), _orientation(r, s, q)
    if ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)):
        return True
    # Endpoint contact and collinear overlap: decided by distance, because real
    # coordinates rarely land exactly on a line.
    return (_near_segment(r, p, q) or _near_segment(s, p, q)
            or _near_segment(p, r, s) or _near_segment(q, r, s))


def _contains(polygon, point):
    """Ray casting, inclusive of the boundary."""
    if any(_near_segment(point, polygon[i - 1], polygon[i]) for i in range(len(polygon))):
        return True
    inside = False
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[i - 1]
        if (yi > point[1]) != (yj > point[1]):
            if point[0] < (xj - xi) * (point[1] - yi) / (yj - yi) + xi:
                inside = not inside
    return inside


@dataclass(frozen=True)
class QueryDomain:
    """The area one task must cover, in metres from the request centre.

    §4.1: the computed boundary expanded by the query margin. The margin covers
    the current error band and entrance offsets; it is an engineering allowance
    and never evidence that the real directory is complete.
    """
    polygon: tuple

    def __post_init__(self):
        points = tuple((float(x), float(y)) for x, y in self.polygon)
        if len(points) < 3 or not all(math.isfinite(v) for p in points for v in p):
            raise ValueError('invalid_query_domain')
        object.__setattr__(self, 'polygon', points)

    @classmethod
    def circle(cls, radius: float, *, segments: int = 72):
        if not math.isfinite(radius) or radius <= 0 or segments < 3:
            raise ValueError('invalid_query_domain')
        step = 2*math.pi / segments
        return cls(tuple((radius*math.cos(i*step), radius*math.sin(i*step))
                         for i in range(segments)))

    @property
    def envelope(self):
        xs = [point[0] for point in self.polygon]
        ys = [point[1] for point in self.polygon]
        return (min(xs), min(ys), max(xs), max(ys))

    def contains(self, point) -> bool:
        return _contains(self.polygon, (float(point[0]), float(point[1])))

    def intersects(self, block: QueryBlock) -> bool:
        x0, y0, x1, y1 = block.bounds
        if any(x0 <= x <= x1 and y0 <= y <= y1 for x, y in self.polygon):
            return True
        corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        if any(self.contains(corner) for corner in corners):
            return True
        edges = list(zip(corners, corners[1:] + corners[:1]))
        return any(_crosses(self.polygon[i - 1], self.polygon[i], edge[0], edge[1])
                   for i in range(len(self.polygon)) for edge in edges)


def coarse_blocks(domain: QueryDomain) -> tuple:
    """§4.2.1: the domain envelope split 2×2, keeping the blocks it intersects.

    Square blocks anchored at the envelope's south-west corner, so a non-square
    envelope is still tiled and the circumscribed radius stays exactly the one
    ``planner.sequence`` computes for a square.
    """
    x0, y0, x1, y1 = domain.envelope
    edge = max(x1 - x0, y1 - y0) / 2
    if edge <= 0:
        raise ValueError('empty_query_domain')
    grid = [QueryBlock(f'r{row}c{col}', x0 + col*edge, y0 + row*edge, edge)
            for row in range(2) for col in range(2)]
    return tuple(block for block in grid if domain.intersects(block))


@dataclass(frozen=True)
class Observation:
    """One provider row, with the request that produced it."""
    provenance: dict
    row: dict


@dataclass
class _Sequence:
    """One (block, category, keyword) query and what it established."""
    block: QueryBlock
    category: str
    query: str
    mapping: dict
    page: int = 0
    page_attempts: int = 0
    status: str = 'pending'
    stop: str | None = None
    pages: list = field(default_factory=list)
    pagination: Pagination = field(default_factory=Pagination)

    def entry(self, source: str) -> dict:
        """One queryCoverage record, derived from the pages actually requested."""
        succeeded = [record for record in self.pages if record['succeeded']]
        return {**self.mapping, 'source': source, 'status': self.status,
                'pages': len(succeeded),
                'returned': sum(record['returned'] for record in succeeded),
                'total': succeeded[-1]['total'] if succeeded else None,
                'truncated': self.stop in ('possible_truncation', 'page_limit'),
                'warnings': sorted({w for record in self.pages for w in record['warnings']}),
                'stopReason': self.stop,
                'requestedPages': [record['pageNum'] for record in self.pages],
                'successfulPages': [record['pageNum'] for record in succeeded],
                'reportedTotals': [{'pageNum': record['pageNum'], 'total': record['total']}
                                   for record in succeeded],
                'pageErrors': [{'pageNum': record['pageNum'], 'reason': record['reason']}
                               for record in self.pages if not record['succeeded']],
                'pageRecords': [dict(record) for record in self.pages]}


@dataclass(frozen=True)
class OnlineResult:
    """What one adaptive run obtained, and what it could not establish."""
    status: str
    coverage: list
    observations: list
    blocks: tuple
    attempts: int
    budget: int
    stop_reason: str | None
    warnings: list
    #: Upstream calls this run actually dispatched. ``attempts`` counts the pages
    #: it *processed*, which a cached replay also is, so the two numbers differ by
    #: exactly the pages the cache answered — that difference is the point of
    #: keeping them apart rather than reporting one as if it were the other.
    network_calls: int = 0
    #: Per category, the bounds (local metres from the origin) of the blocks whose
    #: keyword evidence is incomplete: nothing there may be read as "none exist".
    incomplete: dict = field(default_factory=dict)

    @property
    def catalog_completeness(self) -> str:
        """Never anything else: a finished keyword search is not a complete directory."""
        return 'unverified'


class OnlinePlanner:
    """§4.2's adaptive planner.

    Deterministic: every decision is a function of the evidence collected so far,
    so the same domain and budget always produce the same schedule.
    """

    def __init__(self, *, domain: QueryDomain, origin, categories, budget: int | None = None,
                 source: str, queries=None, finest_edge: float = FINEST_BLOCK_METERS,
                 max_pages: int = MAX_PAGES, token=None, limits: RunLimits | None = None,
                 deadline: float | None = None, checkpoint=None, on_checkpoint=None, spent=None):
        # ``budget`` alone is the ceiling every earlier caller named: pages scheduled,
        # cached ones included. A caller that brings its own ``limits`` says the two
        # ceilings apart instead, and ``budget`` then only reports the network
        # allowance this run was given (``None`` when the service pool is the only
        # thing holding it).
        if limits is None:
            self.limits = (legacy_limits(budget) if spent is None else
                           RunLimits(PROCESSING_STEP_LIMIT))
            self.budget = budget
        else:
            if budget is not None and (type(budget) is not int or budget < 0):
                raise ValueError('budget must be a non-negative integer')
            self.limits = limits
            self.budget = limits.processing_steps if budget is None else budget
        if not categories:
            raise ValueError('at least one category required')
        for category in categories:
            if category not in RULES['queries']:
                raise ValueError('unknown_category')
        self.domain, self.origin, self.categories = domain, origin, tuple(categories)
        self.queries = {category: tuple((queries or {}).get(category, RULES['queries'][category]))
                        for category in self.categories}
        if any(not values for values in self.queries.values()):
            raise ValueError('at least one query required per category')
        self.query_plan_limited = any(self.queries[key] != tuple(RULES['queries'][key])
                                      for key in self.categories)
        self.source = source
        self.finest_edge, self.max_pages = finest_edge, max_pages
        self.token = token or CancelToken()
        self.on_checkpoint, self.spent = on_checkpoint, spent
        self.active = None
        self.deadline = deadline
        self.projection = LocalProjection((origin[0], origin[1]))
        # Structure first: a domain with no extent is empty, not out of range.
        self.coarse = coarse_blocks(domain)
        self._window()
        if not self.coarse:
            raise ValueError('empty_query_domain')
        self.blocks = {block.tile_id: block for block in self.coarse}
        self.children, self.sequences, self.queue = {}, {}, deque()
        self.later = {major: deque() for major in catalog.majors()}
        self.major_order = tuple(major for major in catalog.majors()
                                 if any(catalog.major_of(category) == major for category in self.categories))
        self.major_cursor = 0
        self.observations, self.attempts, self.stopped, self.warnings = [], 0, None, []
        # First give every major a first minor on every block. Then rotate the
        # remaining primary minor queries. Supplements and follow-up pages share
        # a later queue, so neither can consume the first-pass opportunity.
        ordered_blocks = sorted(self.coarse, key=lambda block: (
            (block.x + block.edge / 2) ** 2 + (block.y + block.edge / 2) ** 2,
            block.tile_id))
        minors = {major: [category for category in self.categories
                          if catalog.major_of(category) == major] for major in self.major_order}
        for index in range(max(map(len, minors.values()))):
            for block in ordered_blocks:
                for major in self.major_order:
                    if index < len(minors[major]):
                        category = minors[major][index]
                        self._enqueue(block, category, self.queries[category][0])
        for block in ordered_blocks:
            for major in self.major_order:
                for category in minors[major]:
                    for query in self.queries[category][1:]:
                        self._enqueue(block, category, query, later=True)
        self.first_round = tuple(self.sequences.values())
        self.network_calls = 0
        self.budget_refusals = 0
        if checkpoint is not None:
            self.restore(checkpoint)

    def checkpoint(self):
        """JSON-safe scheduling state, independent of the per-round allowance."""
        key = lambda state: state.mapping['sequenceId']
        sequences = []
        for state in self.sequences.values():
            value = {**vars(state), 'block': vars(state.block).copy(),
                     'mapping': copy.deepcopy(state.mapping), 'pages': copy.deepcopy(state.pages),
                     'pagination': {**vars(state.pagination)}}
            value['pagination']['fingerprints'] = sorted(state.pagination.fingerprints)
            value['pagination']['seen_uids'] = sorted(state.pagination.seen_uids)
            value['pagination']['warnings'] = list(state.pagination.warnings)
            sequences.append(value)
        return {'version': 1, 'domain': self.domain.polygon, 'origin': list(self.origin),
                'categories': list(self.categories), 'source': self.source,
                'blocks': [asdict(block) for block in self.blocks.values()],
                'children': {key: [b.tile_id for b in children] for key, children in self.children.items()},
                'sequences': sequences, 'queue': ([key(self.active)] if self.active else [])
                    + [key(s) for s in self.queue],
                'later': {major: [key(s) for s in queue] for major, queue in self.later.items()},
                'majorCursor': self.major_cursor, 'observations': [dict(provenance=o.provenance.copy(), row=copy.deepcopy(o.row)) for o in self.observations],
                'warnings': list(self.warnings)}

    def restore(self, value):
        if (value['version'] != 1 or tuple(value['categories']) != self.categories
                or tuple(value['origin']) != tuple(self.origin)
                or tuple(map(tuple, value['domain'])) != self.domain.polygon
                or value['source'] != self.source):
            raise ValueError('incompatible_query_checkpoint')
        self.blocks = {v['tile_id']: QueryBlock(**v) for v in value['blocks']}
        self.children = {key: tuple(self.blocks[i] for i in ids) for key, ids in value['children'].items()}
        self.sequences = {}
        by_id = {}
        for raw in value['sequences']:
            raw = copy.deepcopy(raw)
            raw['block'] = self.blocks[raw['block']['tile_id']]
            pagination = raw['pagination']
            for name in ('fingerprints', 'seen_uids'):
                pagination[name] = set(pagination[name])
            raw['pagination'] = Pagination(**pagination)
            state = _Sequence(**raw)
            state.page_attempts = 0
            self.sequences[(state.block.tile_id, state.category, state.query)] = state
            by_id[state.mapping['sequenceId']] = state
        self.queue = deque(by_id[i] for i in value['queue'])
        self.later = {major: deque(by_id[i] for i in ids) for major, ids in value['later'].items()}
        queued = {id(s) for s in self.queue} | {id(s) for q in self.later.values() for s in q}
        for state in self.sequences.values():
            if state.stop in RETRY_ERRORS | FATAL | {'cancelled', 'budget_exhausted', 'unfinished'}:
                state.status, state.stop = ('more' if state.pagination.pages else 'pending'), None
                if id(state) not in queued:
                    self.later[catalog.major_of(state.category)].append(state)
        self.major_cursor = value['majorCursor']
        self.observations = [Observation(**v) for v in value['observations']]
        self.warnings = list(value['warnings'])

    async def _save(self):
        if self.on_checkpoint:
            # Await every durable checkpoint before dispatching the next page.
            # The planner cannot mutate while this thread owns its serialization.
            saved = asyncio.create_task(asyncio.to_thread(
                lambda: self.on_checkpoint(self.checkpoint())))
            try:
                await asyncio.shield(saved)
            except asyncio.CancelledError:
                # Finish the in-flight write before changing scheduling state.
                await saved
                raise

    def _used(self):
        return self.attempts if self.spent is None else self.spent()
    def _window(self):
        x0, y0, x1, y1 = self.domain.envelope
        southwest = self.projection.to_geographic((x0, y0))
        northeast = self.projection.to_geographic((x1, y1))
        if not (-180 <= southwest[0] < northeast[0] <= 180 and -85 < southwest[1] < northeast[1] < 85):
            raise ValueError('window outside supported projection')

    def _enqueue(self, block, category, query, *, later=False):
        key = (block.tile_id, category, query)
        if key in self.sequences:
            return None
        state = _Sequence(block=block, category=category, query=query,
                          mapping=sequence(block.tile_id, block.x, block.y, block.edge,
                                           category, query, self.projection))
        self.sequences[key] = state
        (self.later[catalog.major_of(category)] if later else self.queue).append(state)
        return state

    def _next_later(self):
        for offset in range(len(self.major_order)):
            index = (self.major_cursor + offset) % len(self.major_order)
            major = self.major_order[index]
            if self.later[major]:
                self.major_cursor = (index + 1) % len(self.major_order)
                return self.later[major].popleft()
        return None

    async def _attempt(self, fetch, mapping, page) -> PageResponse:
        """One call, with the scheduling layer's refusals turned into reasons.

        A refusal is reported as ``refused``: nothing reached the network, so it
        costs no allowance and the run's own accounting must not count it as one.
        A source that answers with a bare ``(payload, reason)`` pair was called and
        therefore dispatched, which is what the live transport always is.

        A failure that belongs to *someone else's* attempt — this caller only awaited
        an identical in-flight request — is reported as ``shared``: the reason is the
        one that attempt ended in, and it is still not a call this run dispatched.
        """
        try:
            answer = await fetch(mapping, page)
        except SharedBuildFailed as shared:
            reason = _shared_reason(shared.cause)
            if reason is None:
                raise shared.cause from None
            return PageResponse(None, reason, SHARED)
        except RequestStopped as exc:
            return PageResponse(None, exc.reason, REFUSED)
        except QuotaError as exc:
            # The scheduling layer refuses *before* it reserves, so nothing went out.
            return PageResponse(None, exc.code, REFUSED)
        except httpx.TimeoutException:
            # A transport failure can only happen after the reservation: the
            # attempt was dispatched and the pool counted it. Recording it as
            # "refused" would quietly under-count what this run spent.
            return PageResponse(None, 'timeout', LIVE)
        except httpx.RequestError:
            return PageResponse(None, 'network_error', LIVE)
        if isinstance(answer, PageResponse):
            return answer
        payload, reason = answer
        return PageResponse(payload, reason, LIVE)

    def _finish(self, state, stop):
        if state.status not in ('pending', 'more'):
            return
        state.stop = stop
        state.status = 'completed' if stop == 'completed' else (
            'partial' if state.pagination.pages else 'failed')
        if stop in DENSITY_SIGNALS:
            self._subdivide(state)

    def _subdivide(self, state):
        """§4.2.4: query the same keyword again in circles that can return more.

        Only the keyword whose evidence is incomplete is repeated: a sibling
        keyword that already completed has covered these children's area too.
        """
        children = state.block.children(self.finest_edge)
        if not children:
            self.warnings.append('finest_block_saturated')
            return
        self.children[state.block.tile_id] = children
        for child in children:
            self.blocks.setdefault(child.tile_id, child)
            self._enqueue(child, state.category, state.query, later=True)

    async def _step(self, fetch, state):
        page = state.page
        state.page_attempts += 1
        self.attempts += 1
        response = await self._attempt(fetch, state.mapping, page)
        if response.dispatched:
            # Counted here, where the call was made: a page the cache answered, or
            # one an identical in-flight call already paid for, is not a new call
            # this run made, whatever the pools' shared counters did.
            self.network_calls += 1
        payload, reason = response.payload, response.reason
        record = {'tileId': state.block.tile_id, 'category': state.category, 'query': state.query,
                  'pageNum': page, 'requested': True, 'succeeded': payload is not None,
                  'source': self.source, 'reason': reason, 'total': None, 'returned': 0,
                  'truncated': False, 'warnings': []}
        metadata = getattr(fetch, 'metadata', lambda *_: {})(state.mapping, page)
        if metadata:
            record.update(fetchSource=metadata.get('source'), obtainedAt=metadata.get('obtainedAt'))
        state.pages.append(record)
        if payload is None:
            # A run that keeps its two ceilings apart reads a spent allowance as one
            # page's outcome, not as the end of the retrieval: the pages already
            # cached cost nothing and must still be delivered (§4.2.5). Every other
            # fatal reason — a throttled service, an exhausted upstream quota, a
            # passed deadline — still ends the run.
            drain = self.limits.drain_after_budget_refusal and reason in BUDGET_REFUSALS
            if reason in FATAL and not drain:
                self.stopped = reason
            if drain:
                self.budget_refusals += 1
            if (reason in RETRYABLE and state.page_attempts < MAX_PAGE_ATTEMPTS
                    and self.attempts < self.limits.processing_steps and self.stopped is None):
                # The same page, inside the same budget. Throttling goes back to
                # the front: its retry is the test of whether the shared cooldown
                # was enough, so it must not queue behind every other page first —
                # otherwise a service that is genuinely throttling would cost one
                # attempt per sequence before the run pauses.
                if reason in THROTTLING:
                    self.queue.appendleft(state)
                else:
                    self.queue.append(state)
                return
            if reason in THROTTLING and self.stopped is None:
                # The bounded retry is spent and the service is still refusing:
                # pause here instead of spending the rest of the budget on pages
                # that would meet the same refusal.
                self.stopped = reason
            self._finish(state, reason)
            return
        before = len(state.pagination.warnings)
        stop = state.pagination.consume(payload, self.max_pages)
        added = state.pagination.warnings[before:]
        record.update(total=state.pagination.total, returned=len(payload['results']),
                      warnings=sorted(added), truncated='possible_truncation' in added)
        provenance = {'tileId': state.block.tile_id, 'category': state.category,
                      'sequenceId': state.mapping['sequenceId'], 'query': state.query, 'pageNum': page}
        self.observations.extend(Observation(provenance, row) for row in payload['results'])
        state.page_attempts = 0
        if stop is None:
            state.page = page + 1
            state.status = 'more'
            self.later[catalog.major_of(state.category)].append(state)
            return
        state.page = page + 1
        self._finish(state, stop)

    async def run(self, fetch: Fetch) -> OnlineResult:
        await self._save()
        try:
            while (self.queue or any(self.later.values())) and self.stopped is None:
                # Yield once per scheduling step. A run made entirely of cache hits
                # never reaches the pool and never awaits anything, so a cancellation
                # or deadline that arrived while it was working would otherwise be
                # seen only after it had already finished the work it was told to stop.
                await asyncio.sleep(0)
                if self.token.cancelled:
                    self.stopped = 'cancelled'
                    break
                # A run that never reaches the pool still has to stop: a path made
                # entirely of cache hits bypasses the quota entry, which is where
                # the deadline is otherwise checked, so it is checked here too.
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    self.stopped = 'deadline_reached'
                    break
                if self.spent is not None and not self.limits.drain_after_budget_refusal and self._used() >= self.budget:
                    self.stopped = 'budget_exhausted'
                    break
                if self.attempts >= self.limits.processing_steps:
                    self.stopped = self.limits.processing_stop
                    break
                state = self.queue.popleft() if self.queue else self._next_later()
                if state.status not in ('pending', 'more'):
                    continue
                self.active = state
                await self._save()
                await self._step(fetch, state)
                self.active = None
                await self._save()
        except asyncio.CancelledError:
            # Cancellation is an outcome of this run, not an exception for the
            # caller to clean up: the evidence already obtained is still reported.
            self.token.cancel()
            self.stopped = 'cancelled'
        await self._save()
        if self.stopped is None and self.budget_refusals:
            # Everything that could still be reached was reached, and what remains
            # needs a new call the allowance did not cover: the run says which of
            # the two ceilings it ran into, and the per-page records keep the
            # precise reason (the task's bucket or the day's).
            self.stopped = self.limits.network_stop
        return self.result()

    def _covered(self, block, category, query):
        """Whether one keyword's evidence over one block is complete.

        A block that completed naturally covers itself. A block that was
        subdivided covers itself only if all four children do, because together
        they tile it — which is what lets a resolved anomaly still read as
        complete evidence rather than as a permanent partial.
        """
        state = self.sequences.get((block.tile_id, category, query))
        if state is not None and state.status == 'completed':
            return True
        children = self.children.get(block.tile_id)
        return bool(children) and all(self._covered(child, category, query) for child in children)

    def _uncovered(self, block, category, query) -> list:
        """The leaf bounds under ``block`` where one keyword's evidence is incomplete."""
        state = self.sequences.get((block.tile_id, category, query))
        if state is not None and state.status == 'completed':
            return []
        children = self.children.get(block.tile_id)
        if children:
            return [bounds for child in children for bounds in self._uncovered(child, category, query)]
        return [block.bounds]

    def incomplete_blocks(self) -> dict:
        """Per category, where any of its keywords did not finish: a place there may
        not have been found, so a gap can rest nowhere within reach of it."""
        return {category: sorted({bounds for query in self.queries[category]
                                  for block in self.coarse
                                  for bounds in self._uncovered(block, category, query)})
                for category in self.categories}

    def result(self) -> OnlineResult:
        """Project unfinished work into report rows without destroying the cursor."""
        coverage = []
        for state in self.sequences.values():
            entry = state.entry(self.source)
            if state.status in ('pending', 'more'):
                entry.update(status='partial' if state.pagination.pages else 'failed',
                             stopReason=(state.pages[-1]['reason'] if state.pages and
                                         not state.pages[-1]['succeeded'] else self.stopped or 'unfinished'))
            coverage.append(entry)
        covered = all(self._covered(block, category, query)
                      for block in self.coarse for category in self.categories
                      for query in self.queries[category])
        attempted = any(state.pagination.pages for state in self.sequences.values())
        if self.token.cancelled or self.stopped == 'cancelled':
            status = 'cancelled'
        elif covered:
            status = 'completed'
        else:
            status = 'partial' if attempted else 'failed'
        warnings = sorted(set(self.warnings)
                          | ({'primary_queries_only'} if self.query_plan_limited else set())
                          | {state.stop for state in self.sequences.values()
                             if state.stop not in (None, 'completed')}
                          | ({self.stopped} if self.stopped else set()))
        return OnlineResult(status=status, coverage=coverage, observations=list(self.observations),
                            blocks=tuple(self.blocks.values()), attempts=self.attempts,
                            budget=self.budget, stop_reason=self.stopped, warnings=warnings,
                            network_calls=self.network_calls,
                            incomplete=self.incomplete_blocks())


def clip_to_domain(records, domain: QueryDomain, origin):
    """§4.2.1: the ``around`` search is a superset; keep the domain's part.

    Takes normalized records, so the caller decides what a row is before this
    decides where it is. The rejected ones keep the shape the B1 pipeline uses
    for records outside its window, so one reader handles both.
    """
    projection = LocalProjection((origin[0], origin[1]))
    inside, outside = [], []
    for record in records:
        point = record['location']
        if domain.contains(projection.to_local((point['lng'], point['lat']))):
            inside.append(record)
        else:
            outside.append({'sourceUid': record['sourceUid'], 'reason': 'outside_query_domain',
                            'provenance': record.get('provenance', [])})
    return inside, outside
