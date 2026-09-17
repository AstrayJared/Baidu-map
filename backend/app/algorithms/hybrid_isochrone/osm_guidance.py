"""Advisory topology, never an authorization to reject a Baidu label."""
import json
import math
from functools import lru_cache
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.ops import transform
from shapely.strtree import STRtree

from ..osm_offline.routing import cutoff_dijkstra
from ..osm_offline.snap import nearest_edge_source
from ...geo.projection import MetricProjection
from .extent import computation_extent, contains


@lru_cache(maxsize=2)
def risk_index(path, mtime_ns, size, crs, version):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("osm_data_version") != version:
        raise ValueError("risk_version_mismatch")
    projection = MetricProjection(crs)
    features = []
    for feature in payload["features"]:
        g = transform(projection.forward.transform, shape(feature["geometry"]))
        if g.is_valid and not g.is_empty:
            features.append((g, feature["properties"]["risk_kind"]))
    return tuple(features), STRtree([g for g, _ in features])


class OsmGuidance:
    def __init__(self, store, origin, config, *, risk_path=None, speed=1.3):
        self.store, self.config, self.speed = store, config, speed
        self.origin, self.distances, self.snap = origin, {}, None
        self.warnings, self.features = [], []
        self.feature_index = STRtree([])
        if store is None:
            self.warnings.append("osm_graph_unavailable")
        else:
            try:
                self.snap = nearest_edge_source(store, origin, speed=speed, max_distance=200)
                self.distances = cutoff_dijkstra(store.graph, self.snap.seeds, self.snap.budget_s)
            except ValueError:
                self.warnings.append("osm_origin_snap_unavailable")
        if risk_path and risk_path.is_file() and store:
            try:
                stat = risk_path.stat()
                self.features, self.feature_index = risk_index(str(risk_path.resolve()), stat.st_mtime_ns,
                    stat.st_size, str(store.projection.crs), store.graph.graph.get("osm_data_version"))
            except (OSError, ValueError, KeyError, TypeError):
                self.features = []
                self.warnings.append("osm_risk_layer_invalid")
        else:
            self.warnings.append("osm_water_railway_barrier_layer_unavailable")

    def inspect(self, xy):
        p = Point(xy)
        radius = self.config.topology_search_radius_m
        kinds = sorted({self.features[int(i)][1] for i in self.feature_index.query(p.buffer(radius), predicate="intersects")})
        score = .8 if any(k in ('water', 'bridge', 'railway', 'major_road') for k in kinds) else 0.
        result = {"available": self.store is not None, "risk_score": score, "risk_kinds": kinds, "reachable": None}
        if self.store is None:
            return result
        store, graph = self.store, self.store.graph
        ids = list(map(int, store.index.query(p.buffer(radius), predicate="intersects")))
        outer = list(map(int, store.index.query(p.buffer(radius * 2), predicate="intersects")))
        components, degrees = set(), []
        for i in ids:
            edge = store.edge_ids[i]
            data = graph.edges[edge]
            components.add(store.component[edge[0]])
            degrees.append(len(set(graph.successors(edge[0])) | set(graph.predecessors(edge[0]))))
            if data.get("bridge") not in (None, False, "no", "0", []):
                kinds.append("bridge")
            if str(data.get("highway")) in ("motorway", "trunk", "motorway_link", "trunk_link"):
                kinds.append("major_road")
        if not ids or len(components) > 1:
            kinds.append("network_discontinuity")
        if outer and (len(ids) / len(outer) < .1 or len(ids) / len(outer) > .7):
            kinds.append("density_change")
        if degrees and max(degrees) >= 4:
            kinds.append("branch")
        if any(k in kinds for k in ("bridge", "major_road", "network_discontinuity", "density_change")):
            score = max(score, .7)
        if "branch" in kinds:
            score = max(score, .4)
        result.update(risk_score=score, risk_kinds=sorted(set(kinds)), nearby_edges=len(ids))
        if not store.edge_ids:
            return result
        i = int(store.index.nearest(p))
        edge = store.edge_ids[i]
        line = store.geometries[i]
        offset, position = p.distance(line), line.project(p)
        result["nearest_road_distance_m"] = offset
        # Guidance only. Off-road connection is explicitly an approximation.
        arrival = self.distances.get(edge[0], math.inf)
        duration = arrival + position / self.speed + (self.snap.time_s if self.snap else 0) + offset / self.speed
        if self.snap:
            for e, start in self.snap.source_intervals:
                if e == edge and position >= start:
                    duration = min(duration, self.snap.time_s + (position - start + offset) / self.speed)
        result.update(reachable=bool(duration <= self.config.time_limit_seconds),
                      approximate_duration_s=duration if math.isfinite(duration) else None,
                      connection_assumption="straight_line_advisory_only")
        return result

    def candidates(self, count):
        if self.store is None or count <= 0:
            return []
        region = computation_extent(self.origin, self.config)
        ids = sorted(map(int, self.store.index.query(region, predicate="intersects")))
        # Spatial binning prevents duplicate directed edges dominating proposals.
        proposals = {}
        for i in ids:
            p = self.store.geometries[i].interpolate(.5, normalized=True)
            if not contains(self.origin, (p.x, p.y), self.config):
                continue
            key = (int((p.x - self.origin[0]) // 150), int((p.y - self.origin[1]) // 150))
            if key not in proposals:
                proposals[key] = (p.x, p.y)
        ranked = [(self.inspect(xy)["risk_score"], key, xy) for key, xy in proposals.items()]
        return [xy for _, _, xy in sorted(ranked, key=lambda r: (-r[0], r[1]))[:count]]
