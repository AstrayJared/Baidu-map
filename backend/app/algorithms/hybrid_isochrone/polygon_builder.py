"""Piecewise-linear binary classification on ALL actual normalized samples.

Unknown vertices and unsupported triangles are masked, not labelled negative.
Triangulation's convex envelope is never returned as the reachable geometry.
"""
import math
from dataclasses import dataclass
from types import SimpleNamespace

from shapely.geometry import MultiPoint, MultiPolygon, Polygon, box
from shapely.ops import triangulate, unary_union
from .extent import computation_extent, contains


def multipolygon(geometry):
    if geometry.is_empty:
        return MultiPolygon([])
    if geometry.geom_type == "Polygon":
        return MultiPolygon([geometry])
    if not hasattr(geometry, 'geoms'):
        return MultiPolygon([])
    return MultiPolygon([p for g in geometry.geoms for p in multipolygon(g).geoms])


@dataclass
class BuiltPolygon:
    geometry: object
    support: object
    unknown: object
    extent: object
    segments: list
    refinement: list
    origin_anchor: dict | None = None


def build_polygon(samples, origin, config, *, anchor_verified_origin=False):
    extent = computation_extent(origin, config)
    if any(not contains(origin, s.xy, config) for s in samples):
        raise ValueError("polygon_sample_outside_extent")
    by_xy = {tuple(s.xy): s for s in samples}
    # A successfully verified departure is itself reachable. Keep this logical
    # anchor separate from queried Baidu samples (no invented API response or
    # duration). An actual negative/unknown observation at the origin wins.
    origin_anchor = None
    verified = [s for s in samples if s.evidence.reachable is not None
                and s.evidence.returned_origin is not None
                and s.evidence.origin_offset_m is not None
                and math.isfinite(s.evidence.origin_offset_m)
                and s.evidence.origin_offset_m <= config.endpoint_offset_limit_m]
    if anchor_verified_origin and verified and tuple(origin) not in by_xy:
        origin_anchor = {"point_id": "origin-anchor", "xy": list(origin),
                         "source": "verified_departure_inference",
                         "evidence_point_ids": [s.point_id for s in verified[:3]],
                         "origin_offset_limit_m": config.endpoint_offset_limit_m,
                         "is_baidu_query": False}
        by_xy[tuple(origin)] = SimpleNamespace(
            xy=tuple(origin), point_id="origin-anchor", evidence=SimpleNamespace(reachable=True),
            guidance={}, refinement_level=0, source="ORIGIN_ANCHOR", disagreement=False)
    positive, support, segments, refinement = [], [], [], []
    for triangle in triangulate(MultiPoint(list(by_xy))):
        coords = list(triangle.exterior.coords)[:-1]
        rows = [by_xy[tuple(p)] for p in coords]
        labels = [s.evidence.reachable for s in rows]
        edges = [(math.dist(coords[i], coords[(i + 1) % 3]), i) for i in range(3)]
        longest, edge_index = max(edges)
        mid = tuple((coords[edge_index][j] + coords[(edge_index + 1) % 3][j]) / 2 for j in range(2))
        mixed = True in labels and False in labels
        risk = max(s.guidance.get("risk_score", 0) for s in rows)
        level = max(s.refinement_level for s in rows) + 1
        can_refine = level <= config.max_refinement_depth and longest > config.boundary_tolerance_m
        if None in labels or longest > config.max_triangle_edge_m:
            # Independent geometry must be able to fill sparse support too.
            if can_refine:
                refinement.append((4 if None in labels else 3, -longest, mid, "unknown_or_sparse_support", level))
            continue
        support.append(triangle)
        if can_refine and mixed and longest > config.mixed_triangle_target_m:
            refinement.append((1, -longest, mid, "mixed_triangle_resolution", level))
        elif can_refine and risk >= config.topology_risk_threshold and longest > config.mixed_triangle_target_m:
            refinement.append((2, -risk, mid, "topology_risk", level))
        # Sutherland-Hodgman clipping of a linear 0/2 field at 1.
        clipped, crossings = [], []
        for i in range(3):
            p, q = coords[i], coords[(i + 1) % 3]
            inside, next_inside = labels[i], labels[(i + 1) % 3]
            if inside:
                clipped.append(p)
            if inside != next_inside:
                crossing = tuple((p[j] + q[j]) / 2 for j in range(2))
                clipped.append(crossing)
                crossings.append(crossing)
        if len(clipped) >= 3:
            positive.append(Polygon(clipped))
        if len(crossings) == 2:
            segments.append({"coordinates": crossings, "sample_ids": [s.point_id for s in rows],
                             "resolution_m": longest, "topology_risk_score": risk,
                             "refined": any(s.refinement_level > 0 or s.source != "GEOMETRIC" for s in rows),
                             "osm_baidu_disagreement": any(s.disagreement for s in rows)})
    supported = multipolygon(unary_union(support).intersection(extent))
    geometry = multipolygon(unary_union(positive).intersection(extent))
    if not geometry.is_valid:
        raise ValueError("invalid_classification_geometry")
    return BuiltPolygon(geometry, supported, multipolygon(extent.difference(supported)), extent,
                        segments, sorted(refinement), origin_anchor)
