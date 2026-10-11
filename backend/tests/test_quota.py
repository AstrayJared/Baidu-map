"""§9: per-service pools, per-task buckets, the daily ledger and the downgrade.

The daily ledger is the application's own budget. Nothing here asserts anything
about what a Baidu account has left.
"""
import asyncio
import time
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from app.analyses import RateGate
from app.config import Settings
from app.quota import (DIRECTION, PLACE, SHANGHAI, BudgetExhausted, DailyBudgetExhausted,
                       DailyLedger, DeadlineReached, MatrixDisabled, Quota, QuotaTier,
                       ServicePool, ServiceTier, TaskBudget)

FAST = 1000.0
SWITCH = datetime(2026, 9, 30, 0, 0, tzinfo=SHANGHAI)


def tier(budget=1600, qps=FAST, label="current"):
    return ServiceTier(qps, qps, budget, label)


def before_switch() -> float:
    return (SWITCH - timedelta(hours=1)).timestamp()


def after_switch() -> float:
    return (SWITCH + timedelta(minutes=1)).timestamp()


def new_ledger(tmp_path, *, clock=time.time):
    ledger = DailyLedger(tmp_path / "q.sqlite3", clock=clock)
    ledger.initialize()
    return ledger


def make_pool(tmp_path, name, *, budget=1600, ledger=None, gate=None, switch_at=SWITCH,
              tier_clock=before_switch):
    return ServicePool(name, gate or RateGate(FAST), ledger or new_ledger(tmp_path),
                       QuotaTier(tier(budget), tier(80, 2, "fallback"), switch_at,
                                 clock=tier_clock))


def settings_with(tmp_path, **overrides):
    # The day ceiling is pinned rather than inherited from the deployment default:
    # this file is about tier arithmetic, and a fixture whose number moves with a
    # config edit would fail for a reason that is not this file's subject. What the
    # shipped default itself is has its own test below.
    overrides.setdefault("baidu_place_daily_budget", 1600)
    # Both tiers are pinned, not just the current one: this file also asserts that
    # crossing the switch instant changes nothing about what may be spent, and a
    # fixture that pinned only one tier would invent the very cliff being denied.
    overrides.setdefault("baidu_fallback_place_daily_budget", 1600)
    return Settings(_env_file=None, quota_ledger_path=tmp_path / "q.sqlite3", **overrides)


# -- independent pools -----------------------------------------------------

def test_the_two_services_have_separate_gates_and_separate_allowances(tmp_path):
    quota = Quota(settings_with(tmp_path, baidu_place_daily_budget=1600), clock=before_switch)
    assert quota.direction.gate is not quota.place.gate
    quota.ledger.reserve(DIRECTION, 10 ** 9, cost=500)
    # A route attempt does not touch the place allowance and vice versa.
    assert quota.ledger.spent(DIRECTION) == 500
    assert quota.ledger.spent(PLACE) == 0
    assert quota.remaining(PLACE) == 1600
    # Only the place service has a daily application budget.
    assert quota.remaining(DIRECTION) is None


def test_the_place_day_budget_refuses_before_the_request_is_sent(tmp_path):
    sent = []

    async def run():
        service = make_pool(tmp_path, PLACE, budget=3)
        for _ in range(3):
            async with service.attempt(time.monotonic() + 10):
                sent.append(1)
        with pytest.raises(DailyBudgetExhausted) as refusal:
            async with service.attempt(time.monotonic() + 10):
                sent.append(1)
        assert (refusal.value.service, refusal.value.budget) == (PLACE, 3)
        assert service.remaining_today() == 0

    asyncio.run(run())
    assert sent == [1, 1, 1]


def test_attempt_refusal_happens_before_any_waiting_or_sending(tmp_path):
    slept, sent = [], []

    async def no_sleep(seconds):
        slept.append(seconds)

    async def run():
        service = make_pool(tmp_path, DIRECTION, gate=RateGate(FAST, sleep=no_sleep))
        budget = TaskBudget(isochrone=200, route=1)
        async with service.attempt(time.monotonic() + 10, budget=budget, pool="route"):
            sent.append(1)
        with pytest.raises(BudgetExhausted) as refusal:
            async with service.attempt(time.monotonic() + 10, budget=budget, pool="route"):
                sent.append(1)
        assert refusal.value.pool == "route" and refusal.value.limit == 1
        assert budget.remaining("route") == 0

    asyncio.run(run())
    assert sent == [1]
    assert all(seconds == 0 for seconds in slept)


# -- task buckets ----------------------------------------------------------

def test_task_buckets_count_attempts_and_never_refund(tmp_path):
    budget = TaskBudget(isochrone=200)
    assert budget.pools() == {"isochrone": 200, "poi": 60, "route": 120, "detail": 20}
    assert budget.consume("poi") == 59
    for _ in range(59):
        budget.consume("poi")
    with pytest.raises(BudgetExhausted):
        budget.consume("poi")
    # The other pools are untouched, and a detail route has its own ceiling.
    assert budget.remaining("route") == 120 and budget.remaining("detail") == 20
    assert budget.state()["poi"] == {"limit": 60, "spent": 60}
    with pytest.raises(KeyError):
        budget.consume("isochrone_extra")


def test_the_standard_task_budget_matches_the_documented_pools(tmp_path):
    quota = Quota(settings_with(tmp_path), clock=before_switch)
    assert quota.task_budget(isochrone=400).pools() == \
        {"isochrone": 400, "poi": 60, "route": 120, "detail": 20}
    assert quota.task_budget(isochrone=200, poi=5, route=6, detail=7).pools() == \
        {"isochrone": 200, "poi": 5, "route": 6, "detail": 7}


def test_two_tasks_keep_their_own_buckets_but_share_the_days_allowance(tmp_path):
    """Back-to-back checkups: the buckets belong to a task, the day's budget does not.

    A task that spends its own POI bucket down to zero must not shorten the next
    task's, and a full bucket must not hand out one extra call once the day's
    allowance is gone. Read the other way round, "one task, one budget" would
    become "one task, one allowance", which is how an account gets overdrawn.
    """
    async def run():
        # The day's allowance is deliberately tiny: otherwise "full bucket, empty
        # ledger" would need 1600 attempts to reach.
        quota = Quota(settings_with(tmp_path, baidu_place_daily_budget=2),
                      clock=before_switch)
        first = quota.task_budget(isochrone=400, poi=1)
        second = quota.task_budget(isochrone=400, poi=1)
        async with quota.place.attempt(time.monotonic() + 10, budget=first, pool="poi") as attempt:
            attempt.outcome(None)
        with pytest.raises(BudgetExhausted):
            async with quota.place.attempt(time.monotonic() + 10, budget=first, pool="poi"):
                pass
        # The exhausted bucket is the first task's alone; the second task's is
        # untouched. A refusal before sending is still counted and not refunded,
        # exactly like a failed send — so one attempt, not two, reached the day.
        assert first.remaining("poi") == 0 and second.remaining("poi") == 1
        assert quota.balance()["services"][PLACE]["spentToday"] == 1

        async with quota.place.attempt(time.monotonic() + 10, budget=second, pool="poi") as attempt:
            attempt.outcome(None)
        # Both tasks' attempts land in the same ledger row: the allowance is the
        # application's, and it is what every task draws down. Two tasks, two
        # attempts, and the day is done — the second attempt took the last unit.
        service = quota.balance()["services"][PLACE]
        assert service["spentToday"] == 2 and service["remainingToday"] == 0

        third = quota.task_budget(isochrone=400, poi=1)
        # A fresh task with a fresh bucket, and nothing left in the day: the
        # refusal names the day, not the bucket, so nobody reads "your task
        # still had 60 POI calls" as "so send them".
        with pytest.raises(DailyBudgetExhausted) as refusal:
            async with quota.place.attempt(time.monotonic() + 10, budget=third, pool="poi"):
                pass
        assert refusal.value.service == PLACE and refusal.value.budget == 2
        assert quota.balance()["services"][PLACE]["spentToday"] == 2

    asyncio.run(run())


# -- the durable ledger ----------------------------------------------------

def test_daily_spend_survives_a_restart_and_a_new_quota_instance(tmp_path):
    settings = settings_with(tmp_path, baidu_place_daily_budget=1600)
    Quota(settings, clock=before_switch).ledger.reserve(PLACE, 1600, cost=25)
    restarted = Quota(settings, clock=before_switch)
    # A restart neither clears the ledger nor replays the requests it paid for.
    assert restarted.ledger.spent(PLACE) == 25
    assert restarted.remaining(PLACE) == 1575


def test_the_day_turns_at_shanghai_midnight_and_yesterday_is_kept(tmp_path):
    now = [before_switch()]
    ledger = new_ledger(tmp_path, clock=lambda: now[0])
    ledger.reserve(PLACE, 1600, cost=1600)
    assert ledger.remaining(PLACE, 1600) == 0
    yesterday = ledger.day()
    now[0] = after_switch()
    assert ledger.day() != yesterday
    assert ledger.spent(PLACE) == 0 and ledger.remaining(PLACE, 1600) == 1600
    assert ledger.spent(PLACE, day=yesterday) == 1600


def test_the_downgrade_lowers_the_ceiling_and_shrinks_the_day_budget(tmp_path):
    now = [before_switch()]
    ledger = new_ledger(tmp_path, clock=lambda: now[0])
    service = make_pool(tmp_path, PLACE, ledger=ledger, tier_clock=lambda: now[0])
    assert service.ceiling() == FAST and service.remaining_today() == 1600

    now[0] = after_switch()
    assert service.tier.active().label == "fallback"
    assert service.ceiling() == 2 and service.remaining_today() == 80
    # The morning's spend is not forgiven by the downgrade.
    ledger.reserve(PLACE, 1600, cost=100)
    assert service.remaining_today() == 0
    with pytest.raises(DailyBudgetExhausted):
        ledger.reserve(PLACE, 80)


def test_the_service_cap_is_authoritative_for_its_pool(tmp_path):
    async def run():
        gate = RateGate(10000)  # a caller asking for a looser rate
        service = make_pool(tmp_path, DIRECTION, gate=gate)
        async with service.attempt(time.monotonic() + 10):
            pass
        assert gate.qps == FAST and gate.interval == 1 / FAST

    asyncio.run(run())


def test_the_configured_qps_cap_is_the_tier_ceiling(tmp_path):
    quota = Quota(settings_with(tmp_path), clock=before_switch)
    assert (quota.direction.ceiling(), quota.place.ceiling()) == (3, 10)
    assert (quota.direction.gate.qps, quota.place.gate.qps) == (3, 10)
    # The switch instant still selects a tier without a restart — that mechanism is
    # unchanged — but the tier it selects no longer grants different ceilings. The
    # downgrade was written for an entitlement expiry the console says has not
    # happened, so "crossing the instant changes what may be sent" is no longer true,
    # and asserting the old 16/8 → 2/2 cliff would be asserting a bug.
    downgraded = Quota(settings_with(tmp_path), clock=after_switch)
    assert quota.tiers.active().label == "current"
    assert downgraded.tiers.active().label == "fallback"
    assert (downgraded.direction.ceiling(), downgraded.place.ceiling()) == (3, 10)
    assert downgraded.tiers.active().place_daily_budget == quota.tiers.active().place_daily_budget


# -- one scheduling point --------------------------------------------------

def test_a_deadline_refusal_never_reaches_the_service(tmp_path):
    sent = []

    async def run():
        service = make_pool(tmp_path, DIRECTION)
        with pytest.raises(DeadlineReached):
            async with service.attempt(time.monotonic() - 1):
                sent.append(1)

    asyncio.run(run())
    assert sent == []


def test_only_one_attempt_per_service_is_in_flight(tmp_path):
    active, depths = [], []

    async def run():
        service = make_pool(tmp_path, DIRECTION)

        async def attempt():
            async with service.attempt(time.monotonic() + 10):
                active.append(1)
                depths.append(len(active))
                await asyncio.sleep(0.01)
                active.pop()

        await asyncio.gather(*(attempt() for _ in range(4)))

    asyncio.run(run())
    assert depths == [1, 1, 1, 1]


def test_queued_expiry_does_not_spend_or_dispatch(tmp_path):
    async def run():
        service = make_pool(tmp_path, PLACE)
        budget = TaskBudget(isochrone=400)
        await service.gate.attempt_lock.acquire()
        try:
            with pytest.raises(DeadlineReached):
                async with service.attempt(time.monotonic() + .03, budget=budget, pool='poi'):
                    pytest.fail('expired request was dispatched')
        finally:
            service.gate.attempt_lock.release()
        assert budget.remaining('poi') == 60
        assert service.remaining_today() == 1600
    asyncio.run(run())


def test_queued_request_respects_previous_response_cooldown(tmp_path):
    async def run():
        now = [1000.0]
        async def sleep(delay):
            now[0] += delay
        gate = RateGate(FAST, clock=lambda: now[0], sleep=sleep)
        service = make_pool(tmp_path, DIRECTION, gate=gate)
        started, release = asyncio.Event(), asyncio.Event()
        sends = []
        async def first():
            async with service.attempt(1100) as attempt:
                sends.append(now[0])
                started.set()
                await release.wait()
                attempt.outcome('rate_limit')
        async def second():
            await started.wait()
            async with service.attempt(1100) as attempt:
                sends.append(now[0])
                attempt.outcome(None)
        a, b = asyncio.create_task(first()), asyncio.create_task(second())
        await started.wait()
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(a, b)
        assert sends[1] - sends[0] >= 1
    asyncio.run(run())


def test_a_rate_limit_cools_the_whole_service_down(tmp_path):
    now = [1000.0]

    async def no_sleep(seconds):
        now[0] += seconds

    async def run():
        gate = RateGate(FAST, sleep=no_sleep, spacing_clock=lambda: now[0])
        service = make_pool(tmp_path, DIRECTION, gate=gate)
        async with service.attempt(time.monotonic() + 10) as attempt:
            attempt.outcome("rate_limit")
        # The existing response-paced gate owns the cooldown; the pool only
        # reports the outcome, so a limit at one destination spaces them all.
        assert gate.next_send >= now[0] + 1

    asyncio.run(run())


def test_an_attempt_that_never_reports_is_not_read_as_a_clean_response(tmp_path):
    now = [1000.0]

    async def no_sleep(seconds):
        now[0] += seconds

    async def run():
        gate = RateGate(FAST, sleep=no_sleep, spacing_clock=lambda: now[0])
        service = make_pool(tmp_path, DIRECTION, gate=gate)
        async with service.attempt(time.monotonic() + 10):
            pass
        # Silence is an interrupted attempt, not a confirmed response.
        assert gate.next_send >= now[0] + 1
        async with service.attempt(time.monotonic() + 10) as attempt:
            attempt.outcome(None)
        # A reported clean response only costs the paced interval.
        assert gate.next_send < now[0] + 1

    asyncio.run(run())


def test_a_pool_reservation_is_the_number_the_balance_reports(tmp_path):
    async def run():
        quota = Quota(settings_with(tmp_path, baidu_place_daily_budget=1600),
                      clock=before_switch)
        async with quota.place.attempt(time.monotonic() + 10) as attempt:
            attempt.outcome(None)
        report = quota.balance()
        assert report["services"][PLACE]["spentToday"] == 1
        assert report["services"][PLACE]["remainingToday"] == 1599

    asyncio.run(run())


# -- configuration ---------------------------------------------------------

def test_both_tiers_carry_the_same_ceiling_and_stay_inside_the_account():
    """Both tiers now carry the account's verified numbers, and the same day ceiling.

    Two corrections meet here. The "current" tier's 1600 was an operator-confirmed
    error — never an entitlement anyone had checked — and the walking gate asked for
    16 QPS against an account that grants 3, which is a 429 waiting for a busy day
    rather than headroom. The switch instant stays (removing a configuration surface
    is its own decision) but it no longer changes behaviour, so the deployment cannot
    silently start or stop spending 20x more at a clock boundary.

    A configured ceiling above the account's is not a budget: it can only fail
    upstream. That is what this test locks, so a later edit has to argue with the
    console rather than with this file.
    """
    from app.config import VERIFIED_ENTITLEMENT

    settings = Settings(_env_file=None)
    assert settings.baidu_place_daily_budget == 2000
    assert settings.baidu_place_daily_budget == settings.baidu_fallback_place_daily_budget
    assert settings.baidu_place_qps <= VERIFIED_ENTITLEMENT["placeQps"]
    assert settings.baidu_fallback_place_qps <= VERIFIED_ENTITLEMENT["placeQps"]
    assert settings.baidu_direction_qps <= VERIFIED_ENTITLEMENT["walkingQps"]
    assert settings.baidu_fallback_direction_qps <= VERIFIED_ENTITLEMENT["walkingQps"]
    # The application's own day budget is a share of what the account grants, not an
    # independent number: the rest is left for the browser SDK and other callers.
    assert settings.baidu_place_daily_budget < VERIFIED_ENTITLEMENT["placeDailyCalls"]


def test_matrix_stays_disabled_for_this_release(tmp_path):
    quota = Quota(settings_with(tmp_path), clock=before_switch)
    assert quota.matrix_enabled is False
    with pytest.raises(MatrixDisabled):
        quota.require_matrix()
    with pytest.raises(ValidationError):
        Settings(_env_file=None, baidu_matrix_enabled=True)


def test_quota_settings_reject_unsafe_values():
    for unsuitable in ({"baidu_place_daily_budget": -1}, {"baidu_direction_qps": 0},
                       {"baidu_place_qps": 0}, {"baidu_direction_max_inflight": 2},
                       {"baidu_place_max_inflight": 0},
                       # An instant without an offset is not an instant.
                       {"baidu_quota_fallback_at": "2026-09-30T00:00:00"},
                       {"baidu_fallback_place_daily_budget": -5}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **unsuitable)


def test_the_balance_reports_an_application_budget_not_an_account_balance(tmp_path):
    quota = Quota(settings_with(tmp_path, baidu_place_daily_budget=1600),
                  clock=before_switch)
    quota.ledger.reserve(PLACE, 1600, cost=26)
    report = quota.balance()
    assert report["tier"] == "current" and report["matrixEnabled"] is False
    assert report["services"][PLACE] == {"qps": 10, "maxInflight": 1, "dailyBudget": 1600,
                                        "spentToday": 26, "remainingToday": 1574}
    assert report["services"][DIRECTION]["dailyBudget"] is None
    assert report["claimsAccountBalance"] is False


def test_day_cap_and_task_budget_are_enforced_independently(tmp_path):
    quota = Quota(settings_with(tmp_path, baidu_place_daily_budget=1600,
                                baidu_fallback_place_daily_budget=1600), clock=after_switch)
    quota.ledger.reserve(PLACE, 1600, cost=80)
    assert quota.remaining(PLACE) == 1520
    assert quota.balance()["services"][PLACE] == {
        "qps": 10, "maxInflight": 1, "dailyBudget": 1600,
        "spentToday": 80, "remainingToday": 1520}
    budget = quota.task_budget(isochrone=400)
    for _ in range(60):
        budget.consume("poi")
    with pytest.raises(BudgetExhausted):
        budget.consume("poi")
    fresh = quota.task_budget(isochrone=400, poi=1)

    async def run():
        async with quota.place.attempt(time.monotonic() + 10,
                                       budget=fresh, pool="poi") as attempt:
            attempt.outcome(None)

    asyncio.run(run())
    assert fresh.remaining("poi") == 0
    assert quota.ledger.spent(PLACE) == 81


def test_the_ledger_refuses_a_nonsense_cost(tmp_path):
    ledger = new_ledger(tmp_path)
    for bad in (0, -1, 1.5, "1"):
        with pytest.raises(ValueError):
            ledger.reserve(PLACE, 100, cost=bad)
    with pytest.raises(KeyError):
        ledger.reserve("matrix", 100)
    assert ledger.spent(PLACE) == 0
