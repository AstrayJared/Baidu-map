"""Per-service quota: separate gates, task buckets and a durable daily ledger.

The two Baidu services are separate pools, so they get separate gates; a route
attempt and a place attempt never consume each other's allowance. Pacing itself
is the existing response-paced ``RateGate`` — this module adds what it does not
own: the per-task buckets, the application-wide daily budget and the tier that
applies right now.

An attempt checks allowances, acquires the service slot within its deadline,
waits for response-paced scheduling, then reserves the daily and task budgets.
Queue expiry does not consume a request; dispatched failures remain counted.
Reserving happens *before* the request is sent,
so failed, retried and sent-but-unclear attempts all stay counted and a process
restart never clears the ledger. The day boundary is Asia/Shanghai.

The ledger is a "this application" budget. The browser SDK, other applications
and the console's own accounting sit outside it, so a remaining balance here is
never a claim about the account's real remaining allowance.

The pools are the outer scheduler: a transport wrapper asks its pool for the
attempt and must not also pace on the service's gate, which would pace twice and
deadlock on the shared in-flight lock.

Every walking-route request of the application -- both boundary engines, the
legacy ``/api/analyses`` and ``/api/v1/analysis/hybrid`` endpoints, verification
and click-detail routes -- paces on the one direction gate, and every place
search on the one place gate. The application-side place daily budget is optional;
it is unset by default. Each gate reads the active tier when it waits, so a
tier switch lowers every caller at once and two limiters can never add up on one
key. The engines and legacy endpoints pace on the gate directly (their own
budgets bound them), so only the stages that reserve through a pool appear in
the daily ledger; the balance below says so.
"""
import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analyses import RateGate
from .request_control import RequestStopped, wait_with_cancel

# The deployment's quota day. Fixed rather than read from the host zone, so a
# container in UTC and a workstation in CST agree on when the day turns.
SHANGHAI = timezone(timedelta(hours=8))

DIRECTION = "direction"
PLACE = "place"
GEOCONV = "geoconv"
SERVICES = (DIRECTION, PLACE, GEOCONV)

# Per-task pools. Attempts are counted, not successes; retries are attempts.
DEFAULT_POI_ATTEMPTS = 60
DEFAULT_ROUTE_ATTEMPTS = 120
DEFAULT_DETAIL_ATTEMPTS = 20

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_spend (
    service TEXT NOT NULL,
    day TEXT NOT NULL,
    spent INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (service, day)
);
"""


class QuotaError(Exception):
    """Base for refusals that must stop an attempt before it is sent."""

    code = "quota_error"

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class BudgetExhausted(QuotaError):
    """The task's own pool for this service is used up."""

    code = "task_budget_exhausted"

    def __init__(self, pool: str, limit: int):
        self.pool, self.limit = pool, limit
        super().__init__(f"{pool} budget exhausted ({limit} attempts)")


class DailyBudgetExhausted(QuotaError):
    """The application's daily allowance for this service is used up."""

    code = "daily_budget_exhausted"

    def __init__(self, service: str, budget: int):
        self.service, self.budget = service, budget
        super().__init__(f"{service} daily application budget exhausted ({budget})")


class MatrixDisabled(QuotaError):
    """The route matrix is billed per origin-destination pair and stays off."""

    code = "matrix_disabled"


class DeadlineReached(QuotaError):
    code = "deadline_reached"

    def __init__(self):
        super().__init__("deadline reached before the request was sent")


class AttemptCancelled(QuotaError):
    """The task was cancelled before this attempt was sent; nothing was reserved."""

    code = "cancelled"

    def __init__(self):
        super().__init__("task cancelled before the request was sent")


def attach_token(session, token):
    """Give a transport session the task's cancel token, checked before every attempt.

    Set as an attribute so injected transports keep their plain signature.
    """
    if token is not None:
        try:
            session.token = token
        except AttributeError:
            pass
    return session


class TieredGate(RateGate):
    """A response-paced gate whose ceiling is the active tier, read at every wait.

    ``cap`` is an optional deployment limit (``ANALYSIS_QPS``); it can only lower
    the tier's ceiling, never raise it.
    """

    def __init__(self, ceiling, *, cap: float | None = None):
        self.ceiling, self.cap = ceiling, cap
        super().__init__(self.current_qps())

    def current_qps(self) -> float:
        qps = self.ceiling()
        return min(qps, self.cap) if self.cap else qps

    def refresh(self) -> None:
        self.qps = self.current_qps()
        self.interval = 1 / self.qps

    async def wait(self, deadline, *, cost=1):
        self.refresh()
        return await super().wait(deadline, cost=cost)


@dataclass(frozen=True)
class ServiceTier:
    """The ceilings that apply at one instant."""
    direction_qps: float
    place_qps: float
    place_daily_budget: int | None
    label: str


@dataclass
class TaskBudget:
    """One task's pools. Shared by every transport the task drives."""
    isochrone: int
    poi: int = DEFAULT_POI_ATTEMPTS
    route: int = DEFAULT_ROUTE_ATTEMPTS
    detail: int = DEFAULT_DETAIL_ATTEMPTS
    spent: dict = field(default_factory=dict)
    # Checkup rounds attach a durable reservation journal at the existing
    # scheduling point. Other callers retain their original in-memory budgets.
    on_reserve: object = None
    on_result: object = None
    request_context: dict = field(default_factory=dict)
    reservations: dict = field(default_factory=dict)

    def pools(self) -> dict:
        return {"isochrone": self.isochrone, "poi": self.poi, "route": self.route,
                "detail": self.detail}

    def remaining(self, pool: str) -> int:
        limits = self.pools()
        if pool not in limits:
            raise KeyError(pool)
        return max(0, limits[pool] - self.spent.get(pool, 0))

    def consume(self, pool: str) -> int:
        """Take one attempt. Never refunded: a failed attempt still cost a call."""
        limits = self.pools()
        if pool not in limits:
            raise KeyError(pool)
        limit = limits[pool]
        used = self.spent.get(pool, 0)
        if used >= limit:
            raise BudgetExhausted(pool, limit)
        if self.on_reserve is not None:
            self.reservations[pool] = self.on_reserve(pool, self.request_context.get(pool, {}))
        self.spent[pool] = used + 1
        return limit - self.spent[pool]

    def state(self) -> dict:
        return {name: {"limit": limit, "spent": self.spent.get(name, 0)}
                for name, limit in self.pools().items()}


class DailyLedger:
    """Durable per-day application spend for each service."""

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._keeper = None

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(LEDGER_SCHEMA)
            if self._keeper is None:
                # Keep WAL alive without retaining every short-lived connection.
                self._keeper = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)

    def close(self):
        if getattr(self, '_keeper', None) is not None:
            self._keeper.close()
            self._keeper = None

    def __del__(self):
        self.close()

    def day(self, now: float | None = None) -> str:
        moment = self.clock() if now is None else now
        return datetime.fromtimestamp(moment, SHANGHAI).date().isoformat()

    def spent(self, service: str, *, day: str | None = None) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT spent FROM daily_spend WHERE service=? AND day=?",
                                     (service, day or self.day())).fetchone()
        return row["spent"] if row else 0

    def remaining(self, service: str, budget: int, *, day: str | None = None) -> int:
        return max(0, budget - self.spent(service, day=day))

    def reserve(self, service: str, budget: int, *, cost: int = 1) -> int:
        """Reserve before sending. Raises when the day's allowance is gone.

        The reservation is a transaction, so two concurrent attempts cannot both
        take the last unit, and a crash after reserving leaves the spend recorded
        rather than silently returned.
        """
        if service not in SERVICES:
            raise KeyError(service)
        if type(cost) is not int or cost <= 0:
            raise ValueError("cost must be a positive integer")
        day = self.day()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT spent FROM daily_spend WHERE service=? AND day=?",
                                     (service, day)).fetchone()
            used = row["spent"] if row else 0
            if used + cost > budget:
                connection.execute("COMMIT")
                raise DailyBudgetExhausted(service, budget)
            connection.execute(
                "INSERT INTO daily_spend (service, day, spent, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(service, day) DO UPDATE SET spent=excluded.spent,"
                " updated_at=excluded.updated_at",
                (service, day, used + cost, self.clock()))
            connection.execute("COMMIT")
        return budget - used - cost


class QuotaTier:
    """Which tier applies now. Re-evaluated at every allocation and startup."""

    def __init__(self, current: ServiceTier, fallback: ServiceTier, switch_at: datetime,
                 *, clock=time.time):
        self.current, self.fallback = current, fallback
        self.switch_at, self.clock = switch_at, clock

    def active(self) -> ServiceTier:
        if datetime.fromtimestamp(self.clock(), SHANGHAI) >= self.switch_at:
            return self.fallback
        return self.current


@dataclass
class Attempt:
    """Handle for one in-flight attempt; the transport reports its outcome here."""
    service: str
    reason: str | None = None
    reported: bool = False

    def outcome(self, reason: str | None) -> None:
        """``None`` is a clean response and only costs the paced interval."""
        self.reason = reason
        self.reported = True


class ServicePool:
    """One service's scheduling point: task bucket, optional day cap, slot, pacing."""

    def __init__(self, name: str, gate: RateGate, ledger: DailyLedger, tier: QuotaTier,
                 *, max_inflight: int = 1):
        self.name, self.gate, self.ledger, self.tier = name, gate, ledger, tier
        self.max_inflight = max_inflight

    def ceiling(self) -> float:
        tier = self.tier.active()
        return tier.direction_qps if self.name == DIRECTION else tier.place_qps

    def _daily_budget(self) -> int | None:
        return self.tier.active().place_daily_budget if self.name == PLACE else None

    def remaining_today(self) -> int | None:
        budget = self._daily_budget()
        return None if budget is None else self.ledger.remaining(self.name, budget)

    @asynccontextmanager
    async def attempt(self, deadline: float, *, budget: TaskBudget | None = None,
                      pool: str | None = None, cost: int = 1, token=None):
        """Wait under the service lock; reserve only immediately before dispatch.

        A cancelled ``token`` stops the attempt at every point before the
        reservation: before queuing, after the slot and after the pacing wait.
        """
        if type(cost) is not int or cost <= 0:
            raise ValueError("attempt cost must be a positive integer")

        def check_allowances():
            if token is not None and token.cancelled:
                raise AttemptCancelled()
            if budget is not None and pool is not None and budget.remaining(pool) < cost:
                raise BudgetExhausted(pool, budget.pools()[pool])
            daily = self._daily_budget()
            if daily is not None and self.ledger.remaining(self.name, daily) < cost:
                raise DailyBudgetExhausted(self.name, daily)

        check_allowances()
        remaining = deadline - self.gate.clock()
        if remaining <= 0:
            raise DeadlineReached()
        try:
            await wait_with_cancel(
                asyncio.wait_for(self.gate.attempt_lock.acquire(), timeout=remaining), token,
                on_abandon=lambda acquired: self.gate.attempt_lock.release() if acquired else None)
        except RequestStopped:
            raise AttemptCancelled() from None
        except asyncio.TimeoutError:
            raise DeadlineReached() from None
        try:
            check_allowances()
            cap = getattr(self.gate, "cap", None)
            self.gate.qps = min(self.ceiling(), cap) if cap else self.ceiling()
            self.gate.interval = 1 / self.gate.qps
            # Waiting before acquiring this lock lets queued callers bypass the
            # previous response's cooldown and can dispatch after their deadline.
            if not await wait_with_cancel(self.gate.wait(deadline, cost=cost), token):
                raise DeadlineReached()
            check_allowances()
            daily = self._daily_budget()
            if daily is not None:
                self.ledger.reserve(self.name, daily, cost=cost)
            if budget is not None and pool is not None:
                for _ in range(cost):
                    budget.consume(pool)
            handle = Attempt(service=self.name)
            try:
                yield handle
            finally:
                self.gate.completed(handle.reason if handle.reported else "interrupted", cost=cost)
        except RequestStopped:
            raise AttemptCancelled() from None
        finally:
            self.gate.attempt_lock.release()


class CoordinatePool(ServicePool):
    """Conversion has its own app ceiling, independent of place/walking tiers."""
    def __init__(self, settings, ledger, tier):
        self.qps = settings.baidu_geoconv_qps
        self.daily = settings.baidu_geoconv_daily_budget
        super().__init__(GEOCONV, RateGate(self.qps), ledger, tier)

    def ceiling(self):
        return self.qps

    def _daily_budget(self):
        return self.daily


class Quota:
    """The single allocation entry for both algorithms and every facility stage."""

    def __init__(self, settings, *, clock=time.time, ledger_path: Path | None = None):
        current = ServiceTier(settings.baidu_direction_qps, settings.baidu_place_qps,
                              settings.baidu_place_daily_budget, "current")
        fallback = ServiceTier(settings.baidu_fallback_direction_qps,
                               settings.baidu_fallback_place_qps,
                               settings.baidu_fallback_place_daily_budget, "fallback")
        self.tiers = QuotaTier(current, fallback, settings.baidu_quota_fallback_at, clock=clock)
        self.ledger = DailyLedger(ledger_path or settings.quota_ledger_path, clock=clock)
        self.ledger.initialize()
        self.matrix_enabled = settings.baidu_matrix_enabled
        cap = settings.analysis_qps
        self.direction = ServicePool(
            DIRECTION, TieredGate(lambda: self.tiers.active().direction_qps, cap=cap), self.ledger,
            self.tiers, max_inflight=settings.baidu_direction_max_inflight)
        self.place = ServicePool(
            PLACE, TieredGate(lambda: self.tiers.active().place_qps, cap=cap), self.ledger,
            self.tiers, max_inflight=settings.baidu_place_max_inflight)
        self.geoconv = CoordinatePool(settings, self.ledger, self.tiers)

    def pool(self, name: str) -> ServicePool:
        if name == DIRECTION:
            return self.direction
        if name == PLACE:
            return self.place
        if name == GEOCONV:
            return self.geoconv
        raise KeyError(name)

    def task_budget(self, *, isochrone: int, poi: int | None = None, route: int | None = None,
                    detail: int | None = None) -> TaskBudget:
        return TaskBudget(isochrone=isochrone,
                          poi=DEFAULT_POI_ATTEMPTS if poi is None else poi,
                          route=DEFAULT_ROUTE_ATTEMPTS if route is None else route,
                          detail=DEFAULT_DETAIL_ATTEMPTS if detail is None else detail)

    def require_matrix(self) -> None:
        """The route matrix is billed per origin-destination pair, not per call."""
        if not self.matrix_enabled:
            raise MatrixDisabled("BAIDU_MATRIX_ENABLED is false for this release")

    def balance(self) -> dict:
        """What the interface may call the application's own remaining budget."""
        active = self.tiers.active()
        day = self.ledger.day()
        return {
            "tier": active.label,
            "tierAppliesFrom": self.tiers.switch_at.isoformat(),
            "day": day,
            "services": {
                GEOCONV: {"qps": self.geoconv.qps, "maxInflight": 1,
                          "dailyBudget": self.geoconv.daily,
                          "spentToday": self.ledger.spent(GEOCONV, day=day),
                          "remainingToday": self.geoconv.remaining_today()},
                DIRECTION: {"qps": active.direction_qps,
                            "maxInflight": self.direction.max_inflight,
                            "dailyBudget": None, "spentToday": None, "remainingToday": None},
                PLACE: {"qps": active.place_qps, "maxInflight": self.place.max_inflight,
                        "dailyBudget": active.place_daily_budget,
                        "spentToday": (self.ledger.spent(PLACE, day=day)
                                       if active.place_daily_budget is not None else None),
                        "remainingToday": self.remaining(PLACE)},
            },
            "matrixEnabled": self.matrix_enabled,
            # The browser SDK and other applications share the real account.
            "claimsAccountBalance": False,
        }

    def remaining(self, service: str) -> int | None:
        return self.pool(service).remaining_today()
