"""Budgeted orchestration with no facility calls or hidden offline fallback."""
import math
import time
from collections import Counter

from shapely.geometry import Point, mapping

from .boundary_search import BoundarySearch
from .cache import EvidenceSession
from .models import Validity
from .polygon_builder import build_polygon, multipolygon
from .sampler import exploratory_points, normal_pairs
from .coverage_builder import build_coverage
from .hard_obstacles import LocalObstacles
from .extent import ALGORITHM_VERSION, contains


class HybridIsochroneProvider:
    def __init__(self, projection, provider, gate, guidance=None, progress=None, obstacles=None):
        self.projection, self.provider, self.gate, self.guidance = projection, provider, gate, guidance
        self.session = None
        self.progress = progress
        self.obstacles = obstacles or LocalObstacles(warnings=['hard_obstacle_layer_unavailable_or_invalid'])

    async def compute(self, origin, config, *, ledger_path=None, token=None, seed_ledger=None):
        session = self.session = EvidenceSession(origin, self.projection, config, self.provider, self.gate,
                                                path=ledger_path, token=token, guidance=self.guidance, progress=self.progress, seed_ledger=seed_ledger)
        search = BoundarySearch(session)
        budget = config.max_baidu_requests
        started = time.perf_counter()
        build_seconds = 0.
        def rebuild():
            nonlocal build_seconds
            tick = time.perf_counter()
            evidence = build_polygon(session.samples, session.origin_xy, config, anchor_verified_origin=True)
            coverage = build_coverage(evidence, session.samples, config, self.obstacles)
            build_seconds += time.perf_counter() - tick
            return evidence, coverage
        # The budget has no separate near-field phase. Establish broad support,
        # then spend the remaining calls on the exterior time boundary.
        radial_limit = math.floor(budget * .7)
        await search.initialize(min(radial_limit, math.floor(budget * .4)))
        await search.angular(min(radial_limit, math.floor(budget * .5)))
        if self.guidance:
            for xy in self.guidance.candidates(max(1, budget // 20)):
                if session.requests_used >= radial_limit or not session.available:
                    break
                await session.query(xy, "OSM_GUIDED", "topology_proposal")
        await search.refine(radial_limit)
        # A small whole-area sample prevents a purely radial mesh; it is not a
        # separately budgeted near-field ring.
        explore_count = max(3, math.floor(budget / 20))
        for xy in exploratory_points(session.origin_xy, config.analysis_half_width_m, explore_count):
            if not session.available:
                break
            await session.query(xy, "GEOMETRIC", "independent_2d_exploration")
        built, coverage = rebuild()
        conflicts = []
        unknown_probes = []
        # Both known time boundaries and unsupported frontier require verification.
        pair_count = max(1, math.floor(budget / 12)) if config.max_refinement_depth else 0
        for pair in normal_pairs(coverage.geometry, config.boundary_tolerance_m, pair_count, include_holes=False):
            for xy in pair:
                if not session.available:
                    break
                if not contains(session.origin_xy, xy, config):
                    continue
                sample = await session.query(xy, "BOUNDARY_REFINEMENT", "boundary_normal_validation", 1)
                # Use the actual normalized request vertex, and distinguish
                # unsupported space from a supported negative prediction.
                previous = (bool(built.geometry.covers(Point(sample.xy)))
                            if sample and built.support.covers(Point(sample.xy)) else None)
                if sample and previous is None:
                    unknown_probes.append(sample.point_id)
                if sample and previous is not None and sample.evidence.reachable is not None and sample.evidence.reachable != previous:
                    conflicts.append({"point_id": sample.point_id, "previous_inside": previous,
                                      "reachable": sample.evidence.reachable})
            if not session.available:
                break
        attempted = set()
        skipped_interior = 0
        while session.available:
            built, coverage = rebuild()
            candidates = []
            for candidate in built.refinement:
                p = Point(candidate[2])
                if coverage.geometry.contains(p) and coverage.geometry.boundary.distance(p) > config.mixed_triangle_target_m:
                    skipped_interior += 1
                    continue
                if coverage.mask.contains(p):
                    continue
                candidates.append(candidate)
            for gap in coverage.unresolved_gaps:
                if not coverage.mask.covers(Point(gap['xy'])):
                    candidates.append((0, -gap['area_m2'], tuple(gap['xy']), 'open_gap_confirmation', 1))
            # Resolve observed conflicts before ordinary mesh refinement.
            for conflict in conflicts:
                row = next(s for s in session.samples if s.point_id == conflict["point_id"])
                if coverage.geometry.contains(Point(row.xy)) and coverage.geometry.boundary.distance(Point(row.xy)) > config.mixed_triangle_target_m:
                    continue
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    xy = row.xy[0] + dx * config.boundary_tolerance_m, row.xy[1] + dy * config.boundary_tolerance_m
                    if row.refinement_level < config.max_refinement_depth:
                        candidates.append((0, 0, xy, "local_classification_conflict", row.refinement_level + 1))
            candidate = next((c for c in sorted(candidates) if session.coordinate(c[2]) not in attempted
                              and session.key(session.coordinate(c[2])) not in session.cache
                              and contains(session.origin_xy, c[2], config)), None)
            if candidate is None:
                break
            _, _, xy, reason, level = candidate
            attempted.add(session.coordinate(xy))
            await session.query(xy, "TOPOLOGY_RISK" if reason == "topology_risk" else "BOUNDARY_REFINEMENT", reason, level)
        built, coverage = rebuild()
        search_info = search.diagnostics()
        warnings = list(self.guidance.warnings if self.guidance else ["osm_guidance_unavailable"])
        warnings.extend(self.obstacles.warnings)
        if coverage.inferred.area > 0:
            warnings.append('continuous_interior_contains_inferred_coverage')
        if coverage.conflicts:
            warnings.append('interior_fill_conflicts_recorded')
        if coverage.unresolved_gaps:
            warnings.append('open_gap_candidates_unresolved')
        if seed_ledger:
            warnings.append("explicit_continuation_prior_samples_retained")
        if session.token.cancelled:
            session.stop_reason = "cancelled"
        if not session.available and not session.stop_reason:
            session.stop_reason = "budget_exhausted" if session.requests_used >= budget else "deadline"
        if session.stop_reason:
            warnings.append(session.stop_reason)
        if search_info["unbracketed_directions"]:
            warnings.append("unbracketed_directions")
        frontier = coverage.geometry.boundary.intersection(built.support.boundary)
        unresolved = bool(frontier.length > 0 or search_info["unbracketed_directions"] or
                          any(s["resolution_m"] > config.mixed_triangle_target_m for s in built.segments))
        if frontier.length:
            warnings.append("unsupported_frontier_not_time_boundary")
        # Six-decimal endpoint rounding leaves edge witnesses just inside the
        # box. Include this narrow band when detecting a clipped time boundary.
        extent_contact = coverage.geometry.boundary.intersection(built.extent.boundary.buffer(1)).length
        edge_positive = any(s.evidence.reachable is True and
                            built.extent.boundary.distance(Point(s.xy)) <= 1 for s in session.samples)
        extent_truncated = bool(extent_contact > 0 or edge_positive)
        if extent_truncated:
            warnings.append("computation_extent_truncated")
        contradictions = [s.point_id for s in session.samples if s.evidence.reachable is False and built.geometry.covers(Point(s.xy))]
        if contradictions:
            raise ValueError("polygon_contradicts_valid_negative_samples")
        quality = "insufficient" if coverage.geometry.is_empty else "partial" if (
            unresolved or extent_truncated or session.stop_reason or self.obstacles.warnings
            or not self.guidance or self.guidance.warnings or coverage.inferred.area > 0 or coverage.conflicts) else "usable"
        public = lambda g: self.projection.public_geometry(multipolygon(g), repair_roundoff=True) if not g.is_empty else None
        boundary_reasons = {"angular_discontinuity", "opposite_label_bracket",
                            "boundary_normal_validation", "mixed_triangle_resolution",
                            "local_classification_conflict", "open_gap_confirmation"}
        result = {"algorithm": "hybrid", "algorithm_version": ALGORITHM_VERSION, "quality": quality,
                  "extent_truncated": extent_truncated,
                  "coverage_policy": "continuous_land_interior",
                  "displayGeometry": public(coverage.shell),
                  "geometry": public(coverage.geometry), "unknown_region": public(built.unknown.difference(coverage.geometry).difference(coverage.mask)),
                  "evidence_geometry": public(built.geometry), "inferred_fill_geometry": public(coverage.inferred),
                  "evidence_unknown_region": public(built.unknown),
                  "computation_extent": public(built.extent), "requests_used": session.requests_used,
                  "valid_baidu_samples": sum(s.evidence.reachable is not None for s in session.samples),
                  "invalid_baidu_samples": sum(s.evidence.validity == Validity.OFFSET for s in session.samples),
                  "unknown_samples": sum(s.evidence.reachable is None for s in session.samples),
                  "boundary_error_estimate": search_info["boundary_error_estimate"],
                  "warnings": sorted(set(warnings)), "stop_reason": session.stop_reason or "refinement_complete",
                  "config": config.model_dump(mode="json"),
                  "diagnostics": {**search_info, "metric_crs": str(self.projection.crs),
                                  "metric_geometry": mapping(coverage.geometry), "metric_support": mapping(built.support),
                                  "metric_evidence_geometry": mapping(built.geometry),
                                  "metric_geometry_semantics": "final_continuous_coverage",
                                  **coverage.diagnostics(),
                                  "hard_obstacle_source": self.obstacles.source,
                                  "hard_obstacle_unresolved": self.obstacles.unresolved,
                                  "unresolved_water_lines_affecting_shell": sum(line.intersects(coverage.shell) for line in self.obstacles.unresolved_lines),
                                  "sampling_reasons": dict(Counter(s.reason for s in session.samples)),
                                  "budget_policy": "no_near_field_reserve_outer_boundary_priority",
                                  "outer_boundary_refinement_requests": sum(
                                      s.reason in boundary_reasons for s in session.samples),
                                  "near_field_requests": 0,
                                  "interior_candidates_skipped_across_rebuilds": skipped_interior,
                                  "timing_seconds": {"geometry_rebuilds": build_seconds, "compute_total": time.perf_counter() - started},
                                  "origin_xy": session.origin_xy, "segments": built.segments,
                                  "outside_candidates_skipped": session.outside_candidates_skipped,
                                  "extent_contact_length_m": extent_contact,
                                  "origin_anchor": built.origin_anchor,
                                  "boundary_conflicts": conflicts, "unsupported_frontier_length_m": frontier.length,
                                  "unknown_boundary_probes": unknown_probes,
                                  "support_area_m2": built.support.area,
                                  "error_estimate_scope": "observed_radial_brackets_only_not_global_bound",
                                  "samples": [s.to_dict() for s in session.samples]}}
        session.flush()
        return result
