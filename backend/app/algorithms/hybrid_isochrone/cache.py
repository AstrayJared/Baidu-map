"""Per-run evidence cache and durable, pre-send request reservations."""
import asyncio
import time

from life_circle.coordinates import normalize
from life_circle.models import CancelToken

from ...geo.coordinates import wgs84_to_bd09
from ...persistence import atomic_dump
from ...request_control import request_slot, RequestStopped
from .models import Evidence, Sample, Validity
from .extent import LEDGER_VERSION, contains


class ReplayMiss(ValueError):
    pass


class EvidenceSession:
    def __init__(self, origin, projection, config, provider, gate, *, path=None, token=None, guidance=None, progress=None, seed_ledger=None):
        self.origin = normalize(origin)
        self.projection, self.config, self.provider, self.gate = projection, config, provider, gate
        self.path, self.token, self.guidance = path, token or CancelToken(), guidance
        self.progress = progress
        self.deadline = time.monotonic() + config.deadline_seconds
        self.cache, self.samples, self.events = {}, [], []
        self.lock = asyncio.Lock()
        self.requests_used, self.failures, self.origin_endpoint_failures = 0, 0, 0
        self.stop_reason = None
        self.outside_candidates_skipped = 0
        self.origin_xy = projection.origin(self.origin)
        self.gate.interval = max(self.gate.interval, 1 / config.request_qps)
        if getattr(provider, "network", False) and path is None:
            raise ValueError("live_requests_require_durable_ledger")
        if seed_ledger is not None:
            self.restore(seed_ledger)
        self.flush()

    def restore(self, ledger):
        """Explicit operator continuation only; prior reservations still cost budget.

        Missing responses are unknown and never resent. Reference labels must
        never be passed here; only the same origin's prior algorithm ledger.
        """
        from .models import HybridConfig
        if ledger.get("version") != LEDGER_VERSION:
            raise ValueError("continuation_algorithm_version_mismatch")
        normalized_ledger_config = HybridConfig(**ledger["config"]).model_dump(mode="json")
        current_config = self.config.model_dump(mode="json")
        # An operator may lower pacing after an upstream rate-limit response.
        # Geometry settings and the cumulative budget must remain identical.
        if current_config['request_qps'] > normalized_ledger_config['request_qps']:
            raise ValueError("continuation_qps_increase_not_allowed")
        normalized_ledger_config['request_qps'] = current_config['request_qps']
        if normalize(ledger["origin"]) != self.origin or normalized_ledger_config != current_config:
            raise ValueError("continuation_input_mismatch")
        self.requests_used = ledger["requests_used"]
        if not 0 <= self.requests_used <= self.config.max_baidu_requests or len(ledger["events"]) != self.requests_used:
            raise ValueError("continuation_budget_mismatch")
        self.events = list(ledger["events"])
        rows = {s["point_id"]: s for s in ledger["samples"]}
        for event in self.events:
            coordinate = normalize(event["request_coordinate"])
            xy = self.projection.origin(coordinate)
            if not contains(self.origin_xy, xy, self.config):
                raise ValueError("continuation_sample_outside_extent")
            row = rows.get(event["point_id"])
            if row:
                raw = dict(row["evidence"])
                raw.pop("reachable", None)
                raw["validity"] = Validity(raw["validity"])
                evidence = Evidence(**raw)
            else:
                evidence = Evidence(reason="interrupted_response_unknown_no_retry")
            info = self.guidance.inspect(xy) if self.guidance else {"available": False, "risk_score": 0, "reachable": None}
            sample = Sample(event["point_id"], xy, coordinate, event["source"], event["reason"],
                            event["refinement_level"], evidence, info)
            self.samples.append(sample)
            self.cache[self.key(coordinate)] = sample
            if (evidence.validity == Validity.OFFSET and evidence.origin_offset_m is not None
                    and evidence.origin_offset_m > self.config.endpoint_offset_limit_m):
                self.origin_endpoint_failures += 1
            else:
                self.origin_endpoint_failures = 0
        if self.origin_endpoint_failures >= self.config.origin_endpoint_failure_streak_limit:
            self.stop_reason = "invalid_origin_endpoint_offset"

    def flush(self):
        if self.path is not None:
            try:
                atomic_dump(self.path, {"version": LEDGER_VERSION, "origin": self.origin,
                        "config": self.config.model_dump(mode="json"), "requests_used": self.requests_used,
                        "stop_reason": self.stop_reason, "events": self.events,
                        "samples": [s.to_dict() for s in self.samples]})
            except OSError:
                self.stop_reason = "persistence_failure"
                raise

    def coordinate(self, xy):
        return normalize(wgs84_to_bd09(*self.projection.inverse.transform(*xy)))

    def key(self, coordinate):
        return self.origin, normalize(coordinate), tuple(self.provider.identity), "bd09ll", "bd09ll", "walking", "steps=1"

    @property
    def available(self):
        return not self.stop_reason and not self.token.cancelled and self.requests_used < self.config.max_baidu_requests and time.monotonic() < self.deadline

    async def query(self, xy, source="GEOMETRIC", reason="probe", level=0, *, request_coordinate=None):
        # Serial lock makes cancellation, reservations and duplicate queries atomic.
        async with self.lock:
            if not contains(self.origin_xy, xy, self.config):
                self.outside_candidates_skipped += 1
                return None
            coordinate = normalize(request_coordinate) if request_coordinate is not None else self.coordinate(xy)
            exact_xy = self.projection.origin(coordinate)
            if not contains(self.origin_xy, exact_xy, self.config):
                self.outside_candidates_skipped += 1
                return None
            key = self.key(coordinate)
            if key in self.cache:
                return self.cache[key]
            if not self.available:
                return None
            point_id = f"p{len(self.samples) + 1:04d}"
            try:
                async with request_slot(self.gate, self.token, self.deadline) as outcome:
                    self.requests_used += 1
                    event = {"point_id": point_id, "source": source, "reason": reason,
                             "refinement_level": level, "request_coordinate": coordinate,
                             "reserved_at": time.time(), "state": "reserved"}
                    self.events.append(event)
                    # If this write fails the provider is never entered.
                    self.flush()
                    event["sent_at"] = time.time()
                    try:
                        evidence = await self.provider.query_walking_time(self.origin, coordinate, self.deadline)
                    except asyncio.CancelledError:
                        self.stop_reason = "cancelled"
                        raise
                    except ReplayMiss:
                        self.stop_reason = "replay_evidence_missing"
                        raise
                    except Exception:
                        # Never persist exception strings containing credentials/URLs.
                        evidence = Evidence(Validity.API_ERROR, "transport_exception")
                    if not isinstance(evidence, Evidence):
                        evidence = Evidence(reason="invalid_provider_result")
                    outcome["reason"] = evidence.reason
                    event.update(state="completed", completed_at=time.time())
            except RequestStopped as exc:
                self.stop_reason = exc.reason
                self.flush()
                return None
            info = self.guidance.inspect(exact_xy) if self.guidance else {"available": False, "risk_score": 0, "reachable": None}
            sample = Sample(point_id, exact_xy, coordinate, source, reason, level, evidence, info)
            self.samples.append(sample)
            self.cache[key] = sample
            failure = evidence.validity in (Validity.API_ERROR, Validity.RATE_LIMIT) and evidence.reason != "no_result"
            self.failures = self.failures + 1 if failure else 0
            invalid_origin = (evidence.validity == Validity.OFFSET and evidence.origin_offset_m is not None
                              and evidence.origin_offset_m > self.config.endpoint_offset_limit_m)
            self.origin_endpoint_failures = self.origin_endpoint_failures + 1 if invalid_origin else 0
            if evidence.reason in ("permission", "quota", "invalid_parameter", "rate_limit"):
                self.stop_reason = evidence.reason
            elif self.origin_endpoint_failures >= self.config.origin_endpoint_failure_streak_limit:
                self.stop_reason = "invalid_origin_endpoint_offset"
            elif self.failures >= self.config.failure_streak_limit:
                self.stop_reason = "consecutive_api_failures"
            # Persistence failure propagates; caller must stop the entire run.
            self.flush()
            if self.progress:
                self.progress(self.requests_used, sample)
            return sample


class ReplayProvider:
    network = False
    identity = ("baidu", "frozen-evidence-replay-v1")

    def __init__(self, ledger):
        self.origin = normalize(ledger["origin"])
        self.rows = {normalize(s["request_coordinate"]): s["evidence"] for s in ledger["samples"]}

    async def query_walking_time(self, origin, destination, deadline):
        if normalize(origin) != self.origin or normalize(destination) not in self.rows:
            raise ReplayMiss("replay_query_not_recorded")
        row = dict(self.rows[normalize(destination)])
        row.pop("reachable", None)
        row["validity"] = Validity(row["validity"])
        return Evidence(**row)
