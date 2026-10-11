"""步行路线传输（§6.3、§9.2）：一次预留换一次真实路线。

核验阶段与"点击设施看路线"问的是同一个问题 —— 从这里走到这家设施要多少米、走不走得通
—— 所以两者共用一条传输、一个服务池和一本账。差别只在从哪个桶里扣：核验扣任务的
``route`` 桶（标准 120 次），点击详情扣 ``detail`` 桶（标准 20 次），两个桶都不会被对方
撑大。

这里的计量点就是配额模块的服务池：一次尝试在同一处检查服务上限、阶段桶、日额度和截止
时间，并在发送前做一次事务性预留。**不再另外走路线闸门** —— 池子已经持有该服务的唯一在途
位置并按上限节流，再叠一层闸门会节流两次并把自己锁死（``quota`` 模块开头写明了这一条）。

直线距离不在这里出现：它只用于候选排序与保守筛选，任何"在服务范围内"的结论都必须由
这条返回的路线距离支撑（§6.3）。
"""
import time
from dataclasses import asdict
from typing import Protocol

import httpx
from life_circle.providers import BaiduProvider
from life_circle.models import RouteObservation

from ..baidu import silence_transport_logs
from ..quota import AttemptCancelled, BudgetExhausted, DeadlineReached
from ..request_control import RequestStopped, wait_with_cancel

#: 任务自己的池名，与 ``quota.TaskBudget`` 的拼写一致。
ROUTE_POOL = 'route'
DETAIL_POOL = 'detail'
#: 单次响应的上限，永远让位于任务截止时间。
MAX_TIMEOUT_SECONDS = 8.0
#: 一次候选最多试两次：重试也是尝试，照样从桶里扣。
RETRY_LIMIT = 2
#: 只有这两种失败值得重试：其余（额度、鉴权、参数、无结果）重试一次就是烧一次预算。
RETRY_REASONS = ('temporary', 'timeout')
#: 撞到这些原因就停掉整个阶段 —— 继续问下去只会把同一份失败重复一遍。
STOP_REASONS = frozenset(('auth', 'configuration', 'permission', 'quota', 'invalid_parameter'))


class RoutesUnavailable(Exception):
    """这个部署建不出路线传输（比如没有 AK）。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class RouteTransport(Protocol):
    identity: str
    network: bool

    def session(self, pool, *, budget, deadline): ...


async def open_online(settings, stack) -> "OnlineRouteTransport":
    """生产传输：一个阶段一个 HTTP 客户端，或者一个带名字的拒绝。"""
    if not settings.ak_configured:
        raise RoutesUnavailable('missing_ak')
    if settings.analysis_provider == 'synthetic':
        # 离线合成模式不接真实服务：即使配了 AK 也不发出去。
        raise RoutesUnavailable('synthetic_mode_offline')
    silence_transport_logs()
    client = await stack.enter_async_context(
        httpx.AsyncClient(trust_env=False, follow_redirects=False))
    return OnlineRouteTransport(client, settings.baidu_map_ak.get_secret_value())


class OnlineRouteTransport:
    """directionlite 步行路线：与旧设施阶段同一个适配器和同一套解析。"""

    identity, api_version, network = 'baidu_direction', 'directionlite/v1/walking', True

    def __init__(self, client, secret):
        self.client, self.secret = client, secret

    def session(self, pool, *, budget, deadline):
        return RouteSession(self, pool, budget=budget, deadline=deadline)

    async def route(self, facility_id, origin, destination, timeout):
        """timeout 是剩余秒数；Provider 接收单调时钟的绝对截止时间。"""
        silence_transport_logs()
        uid = facility_id.removeprefix('baidu_place:') if facility_id.startswith('baidu_place:') else None
        provider = BaiduProvider(self.secret, client=self.client,
                                 destination_uid=uid or None, route_metric='distance')
        return await provider.query_walking_time(origin, destination, time.monotonic() + timeout)


class RouteSession:
    """一个任务的路线尝试，绑在它自己的池、桶和截止时间上。

    调用一次就是"把这个设施核验掉"：拿到一条可用观测就返回，值得重试的失败在原位重试，
    每次都单独预留。撞上停止类原因或预算耗尽时，后续候选会拿到一个带原因的拒绝而不是
    静默的空结果。
    """

    def __init__(self, transport, pool, *, budget, deadline, token=None):
        self.transport, self.pool, self.budget, self.deadline = transport, pool, budget, deadline
        # The task's cancel token, checked by the pool before every attempt is sent.
        self.token = token
        self.attempts = 0
        self.stop_reason: str | None = None

    @property
    def identity(self) -> str:
        """写进核验证据的提供方：阶段只认这个名字，不认传输怎么实现。"""
        return self.transport.identity

    @property
    def network(self) -> bool:
        return bool(getattr(self.transport, 'network', True))

    async def __call__(self, facility_id, origin, destination, *, pool: str = ROUTE_POOL):
        value = None
        for _ in range(RETRY_LIMIT):
            if self.stop_reason is not None:
                return None
            timeout = self.deadline - time.monotonic()
            if timeout <= 0:
                self.stop_reason = 'deadline'
                return None
            try:
                if self.budget is not None:
                    self.budget.request_context[pool] = {'facilityId': facility_id,
                                                         'origin': origin, 'destination': destination}
                # The token is passed only when there is one, so an injected pool keeps
                # its plain signature.
                extra = {} if self.token is None else {'token': self.token}
                async with self.pool.attempt(self.deadline, budget=self.budget,
                                             pool=pool, **extra) as attempt:
                    timeout = self.deadline - time.monotonic()
                    if timeout <= 0:
                        attempt.outcome('deadline')
                        raise DeadlineReached()
                    self.attempts += 1
                    value = await wait_with_cancel(self.transport.route(
                        facility_id, origin, destination,
                        min(MAX_TIMEOUT_SECONDS, timeout)), self.token)
                    if self.token is not None and self.token.cancelled:
                        raise RequestStopped('cancelled')
                    attempt.outcome(value.reason)
                    if self.budget is not None and self.budget.on_result is not None:
                        self.budget.on_result(self.budget.reservations[pool], asdict(value), value.reason)
            # 桶用尽和到点都不是"这条路走不通"，而是"这次没有结论"：停下来，把原因
            # 带到证据里去，而不是让异常从阶段里冒出来变成一次失败。
            except BudgetExhausted:
                self.stop_reason = 'task_budget_exhausted'
                return None
            except DeadlineReached:
                self.stop_reason = 'deadline'
                return None
            except (AttemptCancelled, RequestStopped):
                # 取消之后不再发出新的请求；已发出的那一次收不回，但不会有下一次。
                self.stop_reason = 'cancelled'
                return None
            if value.reason in STOP_REASONS:
                self.stop_reason = value.reason
                break
            if value.reason not in RETRY_REASONS:
                break
        return value


class ReplayRoutes:
    """Reuse exact successful route requests after a restart, with their original time."""
    def __init__(self, session, *, store, task_id, budget):
        self.session, self.store, self.task_id, self.budget = session, store, task_id, budget
        self.saved = {self.key(c['facilityId'], c['origin'], c['destination']): value
                      for c, value in store.saved_routes(task_id)
                      if all(k in c for k in ('facilityId', 'origin', 'destination'))}

    @staticmethod
    def key(facility_id, origin, destination):
        return facility_id, tuple(origin), tuple(destination)

    def __getattr__(self, name):
        return getattr(self.session, name)

    def available(self, facility_id, origin, destination):
        return (self.key(facility_id, origin, destination) in self.saved
                or self.budget.remaining(ROUTE_POOL) > 0)

    async def __call__(self, facility_id, origin, destination, *, pool=ROUTE_POOL):
        key = self.key(facility_id, origin, destination)
        if key in self.saved:
            value = dict(self.saved[key])
            for name in ('destination', 'route_origin', 'route_destination', 'request_origin'):
                if value.get(name) is not None:
                    value[name] = tuple(value[name])
            return RouteObservation(**value)
        self.budget.request_context[pool] = {'facilityId': facility_id, 'origin': origin,
                                             'destination': destination}
        before = self.budget.reservations.get(pool)
        observation = await self.session(facility_id, origin, destination, pool=pool)
        reservation = self.budget.reservations.get(pool)
        if observation is not None and reservation is not None and reservation != before:
            self.store.complete_request(reservation, asdict(observation), observation.reason)
            if observation.reason is None:
                self.saved[key] = asdict(observation)
        return observation
