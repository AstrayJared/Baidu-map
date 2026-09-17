"""Bounded deterministic batches. Budgets count every actual provider invocation."""
import asyncio
import time
from dataclasses import replace

from .coordinates import normalize
from .models import RouteObservation, Statistics


class Clock:
    def time(self):
        return time.monotonic()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class Scheduler:
    def __init__(self, request, provider, cancel_token, clock=None, on_progress=None):
        self.request, self.provider, self.token = request, provider, cancel_token
        if provider.network and request.qps is None:
            raise ValueError("真实 Provider 必须显式配置实际 QPS")
        self.clock = clock or Clock()
        self.started = self.clock.time()
        self.deadline = self.started + request.deadline_seconds
        self.next_send = self.started
        self.origin = normalize(request.origin)
        self.namespace = (provider.identity, self.origin, request.coordinate_system)
        self.cache = {self.origin: RouteObservation(self.origin, 0, request_origin=self.origin)}
        self.stats = Statistics(unique_positions=1)
        self.stop_reason = None
        self.closed = False
        self.fail_streak = 0
        self._draining = set()
        # One owner schedules a batch; concurrent callers wait for and reuse its cache.
        self.lock = asyncio.Lock()
        self.on_progress = on_progress

    @property
    def remaining(self):
        return self.request.budget - self.stats.requests

    def _stopped(self):
        if self.token.cancelled:
            self.stop_reason = "cancelled"
        elif self.clock.time() >= self.deadline:
            self.stop_reason = "deadline"
        return self.closed or self.stop_reason is not None

    def close(self):
        self.closed = True

    def _consume_late(self, task):
        self._draining.discard(task)
        if not task.cancelled():
            task.exception()  # Consume errors without logging credential-bearing text.

    async def query(self, destination):
        return (await self.observe_many([destination]))[0]

    async def _invoke(self, destination, attempt):
        started = self.clock.time()
        response_task = asyncio.create_task(self.provider.query_walking_time(self.origin, destination, self.deadline))
        cancel_task = asyncio.create_task(self.token.event.wait())
        try:
            done, _ = await asyncio.wait(
                [response_task, cancel_task],
                timeout=max(0, min(self.request.timeout, self.deadline - started)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._stopped() or cancel_task in done:
                return RouteObservation(destination, reason=self.stop_reason or "cancelled", attempts=attempt)
            if response_task not in done:
                return RouteObservation(destination, reason="timeout", attempts=attempt)
            try:
                observation = response_task.result()
                if not isinstance(observation, RouteObservation) or observation.destination != destination:
                    return RouteObservation(destination, reason="invalid_response", attempts=attempt)
                return replace(observation, attempts=attempt, request_origin=self.origin)
            except Exception:
                # Never expose exception text: HTTP errors can contain credential URLs.
                return RouteObservation(destination, reason="provider_error", attempts=attempt)
        finally:
            for task in (response_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            done, _ = await asyncio.wait([response_task], timeout=.01)
            if response_task in done:
                self._consume_late(response_task)
            else:
                # A cancellation-resistant provider must not delay task completion
                # or allow new sends to exceed the still-live concurrency limit.
                if self.stop_reason is None:
                    self.stop_reason = "upstream_failure"
                self._draining.add(response_task)
                response_task.add_done_callback(self._consume_late)
            latency = self.clock.time() - started
            self.stats.latencies.append(latency)
            if self.provider.network:
                self.stats.network_wait_seconds += latency

    async def _invoke_many(self, destinations, attempt):
        started = self.clock.time()
        response_task = asyncio.create_task(
            self.provider.query_walking_times(self.origin, destinations, self.deadline)
        )
        cancel_task = asyncio.create_task(self.token.event.wait())
        fallback = lambda reason: [RouteObservation(point, reason=reason, attempts=attempt) for point in destinations]
        try:
            done, _ = await asyncio.wait(
                [response_task, cancel_task],
                timeout=max(0, min(self.request.timeout, self.deadline - started)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._stopped() or cancel_task in done:
                return fallback(self.stop_reason or "cancelled")
            if response_task not in done:
                return fallback("timeout")
            try:
                observations = response_task.result()
            except Exception:
                return fallback("provider_error")
            if not isinstance(observations, list) or len(observations) != len(destinations):
                return fallback("invalid_response")
            validated = []
            for point, observation in zip(destinations, observations):
                if not isinstance(observation, RouteObservation) or observation.destination != point:
                    validated.append(RouteObservation(point, reason="invalid_response", attempts=attempt))
                else:
                    validated.append(replace(observation, attempts=attempt, request_origin=self.origin))
            return validated
        finally:
            for task in (response_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            done, _ = await asyncio.wait([response_task], timeout=.01)
            if response_task in done:
                self._consume_late(response_task)
            else:
                if self.stop_reason is None:
                    self.stop_reason = "upstream_failure"
                self._draining.add(response_task)
                response_task.add_done_callback(self._consume_late)
            latency = self.clock.time() - started
            self.stats.latencies.append(latency)
            if self.provider.network:
                self.stats.network_wait_seconds += latency

    def _record(self, point, observation):
        observation = replace(observation, request_origin=self.origin)
        if self._stopped() and observation.duration is not None:
            observation = replace(observation, duration=None, reason=self.stop_reason or "closed")
        self.cache[point] = observation
        if observation.reason in ("temporary", "timeout", "rate_limit") and observation.attempts == self.request.max_attempts:
            self.fail_streak += 1
        elif observation.duration is not None or observation.reason not in ("temporary", "timeout", "rate_limit", "phase_budget", "budget"):
            self.fail_streak = 0
        if self.fail_streak >= 5 and self.stop_reason is None:
            self.stop_reason = "upstream_failure"

    async def _observe_batches(self, missing, call_limit, request_limit):
        batch_size = min(50, max(1, int(getattr(self.provider, "batch_size", 1))))
        for start in range(0, len(missing), batch_size):
            if self._stopped() or self.stats.requests >= call_limit:
                break
            group = missing[start:start + batch_size]
            pending, results = group, {}
            for attempt in range(1, self.request.max_attempts + 1):
                capacity = call_limit - self.stats.requests
                sent = pending[:capacity]
                if not sent or self._stopped():
                    break
                self.stats.requests += len(sent)
                self.stats.network_requests += 1
                self.stats.retries += len(sent) if attempt > 1 else 0
                if self.on_progress:
                    self.on_progress()
                observations = await self._invoke_many(sent, attempt)
                for point, observation in zip(sent, observations):
                    results[point] = observation
                    if observation.reason:
                        counts = self.stats.failures
                        counts[observation.reason] = counts.get(observation.reason, 0) + 1
                    if observation.reason in ("permission", "quota", "invalid_parameter"):
                        self.stop_reason = observation.reason
                pending = [point for point in sent if results[point].reason in ("temporary", "timeout", "rate_limit")]
                if not pending or self._stopped():
                    break
            for point in group:
                observation = results.get(point, RouteObservation(
                    point, reason=self.stop_reason or ("phase_budget" if request_limit is not None else "budget")
                ))
                self._record(point, observation)
            if self.remaining == 0 and self.stop_reason is None:
                self.stop_reason = "budget"

    async def observe_many(self, destinations, *, request_limit=None):
        points = [normalize(p) for p in destinations]
        async with self.lock:
            call_limit = self.request.budget if request_limit is None else min(self.request.budget, self.stats.requests + max(0, request_limit))
            unique = list(dict.fromkeys(points))
            missing = [p for p in unique if p not in self.cache]
            self.stats.cache_hits += len(points) - len(missing)
            if missing and callable(getattr(self.provider, "query_walking_times", None)) and getattr(self.provider, "batch", False):
                await self._observe_batches(missing, call_limit, request_limit)
                self.stats.unique_positions = len(self.cache)
                return [self.cache.get(p, RouteObservation(
                    p, reason=self.stop_reason or ("phase_budget" if request_limit is not None else "closed"),
                    request_origin=self.origin,
                )) for p in points]
            for start in range(0, len(missing), self.request.concurrency):
                if self._stopped() or self.stats.requests >= call_limit:
                    break
                group = missing[start:start + self.request.concurrency]
                pending = group
                results = {}
                for attempt in range(1, self.request.max_attempts + 1):
                    tasks = []
                    sent = []
                    for point in pending:
                        if self._stopped() or self.stats.requests >= call_limit:
                            break
                        delay = max(0, self.next_send - self.clock.time())
                        while delay and not self._stopped():
                            if self.clock.time() + delay >= self.deadline:
                                self.stop_reason = "deadline"
                                break
                            # Wake rate-limit waiting immediately on user cancellation.
                            sleeper = asyncio.create_task(self.clock.sleep(delay))
                            cancelled = asyncio.create_task(self.token.event.wait())
                            await asyncio.wait([sleeper, cancelled], return_when=asyncio.FIRST_COMPLETED)
                            for task in (sleeper, cancelled):
                                if not task.done():
                                    task.cancel()
                            await asyncio.gather(sleeper, cancelled, return_exceptions=True)
                            # An early timer callback must not authorize a send.
                            delay = max(0, self.next_send - self.clock.time())
                        if self._stopped():
                            break
                        # No await between final guard and budget reservation.
                        self.stats.requests += 1
                        self.stats.network_requests += int(self.provider.network)
                        self.stats.retries += int(attempt > 1)
                        self.next_send = self.clock.time() + (1 / self.request.qps if self.request.qps else 0)
                        sent.append(point)
                        tasks.append(asyncio.create_task(self._invoke(point, attempt)))
                        # Start the transport before scheduling the next rate-limited send.
                        await asyncio.sleep(0)
                        if self.on_progress:
                            self.on_progress()
                    observations = await asyncio.gather(*tasks)
                    for point, observation in zip(sent, observations):
                        results[point] = observation
                        if observation.reason:
                            counts = self.stats.failures
                            counts[observation.reason] = counts.get(observation.reason, 0) + 1
                        if observation.reason in ("permission", "quota", "invalid_parameter"):
                            self.stop_reason = observation.reason
                    pending = [p for p in sent if results[p].reason in ("temporary", "timeout", "rate_limit")]
                    if not pending or self._stopped():
                        break
                for point in group:
                    observation = results.get(point, RouteObservation(point, reason=self.stop_reason or ("phase_budget" if request_limit is not None else "budget")))
                    self._record(point, observation)
                if self.remaining == 0 and self.stop_reason is None:
                    self.stop_reason = "budget"
            self.stats.unique_positions = len(self.cache)
            return [self.cache.get(p, RouteObservation(p, reason=self.stop_reason or ("phase_budget" if request_limit is not None else "closed"), request_origin=self.origin)) for p in points]
