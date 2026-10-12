"""Versioned JSON+gzip cache: data only, never executable pickle.

All validation/normalization and spatial indexing happen once at startup.
"""
import gzip
import json
import math
from pathlib import Path

import networkx as nx
from shapely import from_wkt
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from ... import bulk_load
from ...geo.projection import MetricProjection

CACHE_VERSION = 1
PEDESTRIAN_ATTRS = ("highway", "foot", "access", "bridge", "tunnel", "service", "oneway:foot")
#: Edges between two progress reports while a cache is built and checked.
PROGRESS_EDGES = 20_000


def _unreported(step, done=None, total=None):
    pass


class OsmDataError(ValueError):
    def __init__(self, reason, diagnostics=None):
        super().__init__(reason)
        self.diagnostics = diagnostics or {}


def edge_travel_time(edge, speed):
    length = float(edge["length"])
    if not math.isfinite(length) or length <= 0 or not math.isfinite(speed) or speed <= 0:
        raise OsmDataError("invalid_edge_cost")
    return length / speed


def oriented_geometry(graph, u, v, data):
    start = Point(graph.nodes[u]["x"], graph.nodes[u]["y"])
    end = Point(graph.nodes[v]["x"], graph.nodes[v]["y"])
    geometry = data.get("geometry")
    fallback = geometry is None
    if fallback:
        geometry = LineString([start, end])
    if not isinstance(geometry, LineString) or not geometry.is_valid or geometry.length <= 0:
        raise OsmDataError("invalid_edge_geometry")
    a, b = Point(geometry.coords[0]), Point(geometry.coords[-1])
    reverse = a.distance(end) + b.distance(start) < a.distance(start) + b.distance(end)
    if reverse:
        geometry = LineString(list(geometry.coords)[::-1])
    if Point(geometry.coords[0]).distance(start) > 1 or Point(geometry.coords[-1]).distance(end) > 1:
        raise OsmDataError("edge_geometry_endpoint_mismatch")
    return geometry, fallback, reverse


class GraphStore:
    def __init__(self, graph, *, speed, crs, progress=_unreported):
        """``progress(step, done, total)`` hears the edge checks as they go: for
        the city graph they are most of its first load, minutes of it."""
        if not isinstance(graph, nx.MultiDiGraph):
            raise OsmDataError("directed_multigraph_required")
        self.projection = MetricProjection(crs)
        if graph.graph.get("crs") is None or self.projection.crs != MetricProjection(graph.graph["crs"]).crs:
            raise OsmDataError("cache_crs_mismatch")
        self.graph = graph.copy()
        self.node_count = self.graph.number_of_nodes()
        self.edge_count = self.graph.number_of_edges()
        self.diagnostics = {"geometry_fallback_edges": 0, "geometry_reversed_edges": 0}
        for _, data in self.graph.nodes(data=True):
            if not all(c in data for c in ("x", "y")):
                raise OsmDataError("cache_node_coordinates_missing")
            if not all(math.isfinite(float(data[c])) for c in ("x", "y")):
                raise OsmDataError("invalid_node_coordinate")
        progress("graph_check", 0, self.edge_count)
        for done, (u, v, k, data) in enumerate(self.graph.edges(keys=True, data=True), 1):
            if done % PROGRESS_EDGES == 0:
                progress("graph_check", done, self.edge_count)
            if "length" not in data or "travel_time_s" not in data:
                raise OsmDataError("cache_cost_attributes_missing")
            geometry, fallback, reverse = oriented_geometry(self.graph, u, v, data)
            data["geometry"] = geometry
            expected = edge_travel_time(data, speed)
            if not math.isclose(float(data["travel_time_s"]), expected, rel_tol=1e-8):
                raise OsmDataError("cache_speed_or_cost_mismatch")
            if not math.isclose(float(data["length"]), geometry.length, rel_tol=1e-6, abs_tol=0.01):
                raise OsmDataError("cache_length_geometry_mismatch")
            for attr in PEDESTRIAN_ATTRS:
                if attr not in data:
                    raise OsmDataError("cache_pedestrian_attributes_missing")
            self.diagnostics["geometry_fallback_edges"] += int(fallback)
            self.diagnostics["geometry_reversed_edges"] += int(reverse)
        progress("graph_index")
        self.edge_ids = sorted(self.graph.edges(keys=True), key=lambda e: tuple(map(str, e)))
        self.geometries = [self.graph.edges[e]["geometry"] for e in self.edge_ids]
        self.index = STRtree(self.geometries)
        self.component = {}
        self.component_sizes = {}
        for i, nodes in enumerate(nx.weakly_connected_components(self.graph)):
            self.component_sizes[i] = len(nodes)
            self.component.update(dict.fromkeys(nodes, i))
        nx.freeze(self.graph)


def save_graph_cache(graph, path):
    payload = {"cache_version": CACHE_VERSION, "metadata": graph.graph,
               "nodes": [[n, d] for n, d in graph.nodes(data=True)],
               "edges": [[u, v, k, {**d, "geometry": d["geometry"].wkt}]
                         for u, v, k, d in graph.edges(keys=True, data=True)]}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_graph_cache(path, progress=_unreported):
    if not Path(path).is_file():
        raise OsmDataError("graph_cache_missing")
    try:
        progress("graph_read")
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            payload = bulk_load.loads(stream.read(), checkpoint=lambda: progress('graph_read'))
        if payload["cache_version"] != CACHE_VERSION:
            raise OsmDataError("graph_cache_version_mismatch")
        graph = nx.MultiDiGraph(**payload["metadata"])
        graph.add_nodes_from(payload["nodes"])
        total = len(payload["edges"])
        progress("graph_build", 0, total)
        for done, (u, v, key, data) in enumerate(payload["edges"], 1):
            if done % PROGRESS_EDGES == 0:
                progress("graph_build", done, total)
            data["geometry"] = from_wkt(data["geometry"])
            if u not in graph or v not in graph or graph.has_edge(u, v, key):
                raise OsmDataError("graph_cache_invalid_topology")
            graph.add_edge(u, v, key=key, **data)
        if len(payload["nodes"]) != graph.number_of_nodes():
            raise OsmDataError("graph_cache_duplicate_nodes")
        for name, actual in (("node_count", graph.number_of_nodes()), ("edge_count", graph.number_of_edges())):
            if name in graph.graph and graph.graph[name] != actual:
                raise OsmDataError("graph_cache_count_mismatch")
        return graph
    except OsmDataError:
        raise
    except Exception:
        raise OsmDataError("graph_cache_corrupt") from None
