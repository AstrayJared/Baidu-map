"""Local opposite-label brackets; never assumes a globally monotone ray."""
import math

from .sampler import radial
from .extent import ray_limit


class BoundarySearch:
    def __init__(self, session):
        self.session, self.config = session, session.config
        self.rays = {}
        for sample in session.samples:
            if sample.reason not in ("radial_probe", "outer_independent_probe", "inner_probe", "angular_discontinuity", "opposite_label_bracket"):
                continue
            dx, dy = sample.xy[0] - session.origin_xy[0], sample.xy[1] - session.origin_xy[1]
            angle = math.atan2(dy, dx) % (2 * math.pi)
            # v1 performs one angular round: initial rays and their mid-angles.
            # max_direction_count caps quantity; it does not define the angles.
            step = math.pi / self.config.initial_direction_count
            angle = (round(angle / step) * step) % (2 * math.pi)
            self.rays.setdefault(angle, {})[math.hypot(dx, dy)] = sample

    async def probe(self, angle, radius, source="GEOMETRIC", reason="radial_probe", level=0):
        sample = await self.session.query(radial(self.session.origin_xy, angle, radius), source, reason, level)
        if sample:
            self.rays.setdefault(angle, {})[radius] = sample
        return sample

    def brackets(self, angle):
        valid = [(r, s) for r, s in sorted(self.rays[angle].items()) if s.evidence.reachable is not None]
        return [(a, b) for a, b in zip(valid, valid[1:]) if a[1].evidence.reachable != b[1].evidence.reachable]

    async def initialize(self, limit):
        angles = [2 * math.pi * i / self.config.initial_direction_count for i in range(self.config.initial_direction_count)]
        # Distance-major order guarantees direction fairness under small budgets.
        for radius in self.config.initial_probe_distances_m:
            for angle in angles:
                if self.session.requests_used >= limit or not self.session.available:
                    return
                await self.probe(angle, radius)
        for angle in angles:
            rows = list(self.rays.get(angle, {}).values())
            values = [s.evidence.reachable for s in rows]
            if values and all(v is True for v in values):
                radius = max(self.rays[angle])
                outer = ray_limit(angle, self.config)
                while radius < outer and self.session.requests_used < limit and self.session.available:
                    radius = min(radius * 1.5, outer)
                    await self.probe(angle, radius, reason="outer_independent_probe")
            elif not any(v is True for v in values):
                radius = self.config.initial_probe_distances_m[0]
                for depth in range(1, min(3, self.config.max_refinement_depth) + 1):
                    if self.session.requests_used >= limit or not self.session.available:
                        break
                    radius /= 2
                    await self.probe(angle, radius, reason="inner_probe", level=depth)

    async def refine(self, limit):
        attempts = set()
        while self.session.available and self.session.requests_used < limit:
            queue = []
            for angle in sorted(self.rays):
                for a, b in self.brackets(angle):
                    width = b[0] - a[0]
                    depth = max(a[1].refinement_level, b[1].refinement_level) + 1
                    if width <= self.config.boundary_tolerance_m or depth > self.config.max_refinement_depth:
                        continue
                    # Unknown at midpoint does not shift the bracket; bounded quarter probes.
                    for fraction in (.5, .25, .75):
                        radius = a[0] + width * fraction
                        key = angle, round(radius, 6)
                        if key not in attempts and radius not in self.rays[angle]:
                            queue.append((-width, angle, radius, depth, key))
                            break
            if not queue:
                return
            _, angle, radius, depth, key = min(queue)
            attempts.add(key)
            await self.probe(angle, radius, "BOUNDARY_REFINEMENT", "opposite_label_bracket", depth)

    async def angular(self, limit):
        angles = sorted(self.rays)
        additions = []
        for a, b in zip(angles, angles[1:] + [angles[0] + 2 * math.pi] if angles else []):
            bkey = b % (2 * math.pi)
            left = [r for r, s in self.rays[a].items() if s.evidence.reachable is True]
            right = [r for r, s in self.rays.get(bkey, {}).items() if s.evidence.reachable is True]
            risk = max([s.guidance.get("risk_score", 0) for key in (a, bkey) for s in self.rays.get(key, {}).values()] or [0])
            jump = left and right and (abs(max(left) - max(right)) > self.config.angular_refinement_threshold_m or abs(max(left) - max(right)) > min(max(left), max(right)) * self.config.angular_refinement_ratio)
            if jump or risk >= self.config.topology_risk_threshold:
                additions.append(((a + b) / 2 % (2 * math.pi), risk))
        for angle, risk in additions[:max(0, self.config.max_direction_count - len(angles))]:
            for radius in self.config.initial_probe_distances_m:
                if self.session.requests_used >= limit or not self.session.available:
                    return
                await self.probe(angle, radius, "TOPOLOGY_RISK" if risk >= self.config.topology_risk_threshold else "BOUNDARY_REFINEMENT", "angular_discontinuity")

    def diagnostics(self):
        brackets = [{"angle": angle, "width_m": b[0] - a[0], "sample_ids": [a[1].point_id, b[1].point_id]}
                    for angle in sorted(self.rays) for a, b in self.brackets(angle)]
        initial = [2 * math.pi * i / self.config.initial_direction_count for i in range(self.config.initial_direction_count)]
        missing = sum(not any(abs(math.atan2(math.sin(a - b), math.cos(a - b))) < 1e-6 for b in self.rays) for a in initial)
        return {"directions": len(self.rays), "brackets": brackets, "unsampled_initial_directions": missing,
                "unbracketed_directions": missing + sum(not self.brackets(a) for a in self.rays),
                "boundary_error_estimate": max((b["width_m"] for b in brackets), default=None)}
