"""§4.2's first round, planned once so a precheck and the run cannot disagree.

A precheck that answers "may this retrieval start, and does it have to go to the
network at all?" has to count the pages the run will really ask for, under the
keys the cache really uses. This module derives both from the planner itself —
the same ``coarse_blocks`` extent, the same rotation, the same category
vocabulary, the same ``page_key`` — instead of restating them approximately. It
*constructs* the planner and reads the round it planned; it never runs it, sends
nothing, reserves nothing and touches no cache entry's data time.

Two limits are stated rather than implied, because both are easy to overread:

* Only each category's primary keyword is planned. Later keywords, paging and
  subdivision cost more, so an affordable first round is not a promise that the
  retrieval finishes.
* A page counted as reusable is one a read-only lookup can answer *now*. An
  identical in-flight request is not one: it has not succeeded yet, so it is
  never netted off the estimate. Execution may still get it for free, which can
  only make the real cost smaller than what was checked here.
"""
from dataclasses import dataclass

from .cache import DEFAULT_COORDINATE_SYSTEM, page_key
from .online import OnlinePlanner
from .planner import RULES as PLAN_RULES


@dataclass(frozen=True)
class InitialPage:
    """One page of the first round: the query that asks for it and its cache key."""

    sequence: dict
    page: int
    key: str


@dataclass(frozen=True)
class InitialPlan:
    """What a retrieval's first round asks for, before anything is sent."""

    pages: tuple[InitialPage, ...]
    blocks: int
    categories: tuple[str, ...]
    #: The round plans each category's primary keyword only.
    primary_queries_only: bool = True

    @property
    def page_count(self) -> int:
        return len(self.pages)


@dataclass(frozen=True)
class InitialEstimate:
    """A precheck's numbers, named the way the contract reports them.

    ``initial_page_count`` is pages, not calls. ``estimated_new_initial_calls`` is
    what would go to the network if nothing else changed — and nothing here is a
    reservation: the cache can be evicted, a cross-task window can lapse and
    another task can spend the day's allowance between this check and the first
    dispatch, which is why the service pool still refuses at the moment of send.
    """

    initial_page_count: int
    reusable_initial_page_count: int
    estimated_new_initial_calls: int
    remaining_task_budget: int
    remaining_daily_budget: int | None
    blocks: int
    minor_categories: int
    primary_queries_only: bool = True

    def as_contract(self) -> dict:
        """The camel-case wire form; an estimate, and it says so."""
        return {
            "initialPageCount": self.initial_page_count,
            "reusableInitialPageCount": self.reusable_initial_page_count,
            "estimatedNewInitialCalls": self.estimated_new_initial_calls,
            "remainingTaskBudget": self.remaining_task_budget,
            "remainingDailyBudget": self.remaining_daily_budget,
            "blocks": self.blocks,
            "minorCategories": self.minor_categories,
            "primaryQueriesOnly": self.primary_queries_only,
            "isReservation": False,
        }


def primary_queries(categories) -> dict:
    """One keyword per minor category: what the first round of each category sends."""
    return {category: tuple(PLAN_RULES['queries'][category][:1]) for category in categories}


def initial_plan(domain, origin, categories, *, provider, api_version,
                 coordinate_system=DEFAULT_COORDINATE_SYSTEM,
                 queries=None, source='plan') -> InitialPlan:
    """The first round the stage will run, as planned by the planner itself.

    ``provider``/``api_version`` are the identity of the transport the run will
    use — declared by that transport, never guessed here — so the keys this
    returns are the keys the execution will look up.
    """
    categories = tuple(categories)
    planner = OnlinePlanner(domain=domain, origin=origin, categories=list(categories),
                            budget=1, source=source,
                            queries=primary_queries(categories) if queries is None else queries)
    pages = tuple(
        InitialPage(sequence=state.mapping, page=0,
                    key=page_key(state.mapping, 0, provider=provider, api_version=api_version,
                                 coordinate_system=coordinate_system))
        for state in planner.first_round)
    return InitialPlan(pages=pages, blocks=len(planner.coarse), categories=categories,
                       primary_queries_only=planner.query_plan_limited)


def reusable_pages(plan: InitialPlan, cache, *, task_id: str) -> int:
    """How many of the plan's pages this cache can answer right now.

    A read-only lookup on purpose: the precheck must not build a page, refresh an
    entry's data time, or join an in-flight request whose outcome nobody has seen.
    """
    return sum(1 for page in plan.pages if cache.get(page.key, task_id=task_id) is not None)


def admission_refusal(estimate: InitialEstimate) -> str | None:
    """Which ceiling — if any — refuses a retrieval *before* it starts.

    One rule, asked by both callers that need the answer: the facility stage and the
    extension endpoint. It is deliberately the same function rather than the same
    sentence written twice, because the two must not disagree about whether work the
    cache can already answer is allowed to run.

    * Nothing new has to leave the process: never refused, whatever the balance is.
    * Something is reusable: allowed to run for the evidence the cache holds; what may
      actually be sent is bounded by the pool at the moment of dispatch, so a short
      budget costs missing pages, not the pages already paid for.
    * Nothing is reusable (a cold first round) and it needs more new calls than the
      allowance covers: refused by name, with its numbers, before anything is queued.
    """
    if estimate.estimated_new_initial_calls <= 0:
        return None
    if estimate.reusable_initial_page_count > 0:
        return None
    if estimate.remaining_task_budget <= 0:
        return 'task_budget_exhausted'
    if estimate.remaining_daily_budget is not None and estimate.remaining_daily_budget <= 0:
        return 'daily_budget_exhausted'
    if estimate.estimated_new_initial_calls > estimate.remaining_task_budget:
        return 'budget_too_small'
    if (estimate.remaining_daily_budget is not None
            and estimate.estimated_new_initial_calls > estimate.remaining_daily_budget):
        return 'daily_budget_exhausted'
    return None


def estimate(plan: InitialPlan, cache, *, task_id: str, remaining_task_budget: int,
             remaining_daily_budget: int | None) -> InitialEstimate:
    """The five numbers a budget decision is made from, all from the same plan."""
    reusable = reusable_pages(plan, cache, task_id=task_id)
    return InitialEstimate(
        initial_page_count=plan.page_count,
        reusable_initial_page_count=reusable,
        estimated_new_initial_calls=plan.page_count - reusable,
        remaining_task_budget=remaining_task_budget,
        remaining_daily_budget=remaining_daily_budget,
        blocks=plan.blocks,
        minor_categories=len(plan.categories),
        primary_queries_only=plan.primary_queries_only)
