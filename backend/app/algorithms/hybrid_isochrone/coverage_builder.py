"""Continuous land interior; inference is recorded separately from evidence."""
from dataclasses import dataclass

from shapely.geometry import MultiPolygon, Point, Polygon, mapping
from shapely.ops import unary_union

from .hard_obstacles import LocalObstacles
from .polygon_builder import multipolygon, build_polygon


def filled_shells(geometry):
    # Union can itself enclose a hole: repeat after merging components.
    merged = multipolygon(unary_union([Polygon(p.exterior) for p in multipolygon(geometry).geoms]))
    return multipolygon(unary_union([Polygon(p.exterior) for p in merged.geoms]))


@dataclass
class Coverage:
    geometry: object
    shell: object
    inferred: object
    mask: object
    corridors: object
    repairs: list
    unresolved_gaps: list
    conflicts: list
    bridge_records: list
    fill_sample_stats: dict

    def diagnostics(self):
        return dict(metric_coverage_shell=mapping(self.shell), metric_inferred_fill=mapping(self.inferred),
                    metric_hard_obstacles=mapping(self.mask), metric_bridge_corridors=mapping(self.corridors),
                    gap_repairs=self.repairs, unresolved_gap_candidates=self.unresolved_gaps,
                    interior_fill_conflicts=self.conflicts,
                    fill_false_positive_samples=self.conflicts,
                    fill_false_positive_count=len(self.conflicts),
                    fill_sample_stats=self.fill_sample_stats,
                    bridge_passages=self.bridge_records,
                    non_obstacle_interior_residual_m2=self.shell.difference(self.mask).difference(self.geometry).area,
                    hard_obstacle_overlap_m2=self.geometry.intersection(self.mask).area,
                    inferred_fill_area_m2=self.inferred.area)


def build_coverage(built, samples, config, obstacles=None, *, repair_gaps=True):
    obstacles = obstacles or LocalObstacles(warnings=['hard_obstacle_layer_unavailable_or_invalid'])
    shell = filled_shells(built.geometry)
    repairs, unresolved, additions = [], [], []
    # Unknown observations create no time boundary. Build a SECONDARY local
    # proposal mesh using valid witnesses only, preserving every known negative.
    # It supplies bounded patch proposals, never replaces the evidence mesh.
    if repair_gaps and any(s.evidence.reachable is None for s in samples):
        center = built.extent.centroid
        valid_mesh = build_polygon([s for s in samples if s.evidence.reachable is not None],
                                   (center.x, center.y), config,
                                   anchor_verified_origin=built.origin_anchor is not None)
        for patch in multipolygon(valid_mesh.geometry.difference(shell)).geoms:
            if patch.area < .01:
                continue
            attachment = patch.boundary.intersection(shell.boundary.buffer(.05)).length / patch.length
            unsupported = patch.intersection(built.unknown).area / patch.area
            if attachment < .55 or unsupported < .99 or patch.area > config.max_triangle_edge_m ** 2 / 4:
                continue
            # At least two actual reachable vertices must border the patch.
            witnesses = [s.point_id for s in samples if s.evidence.reachable is True
                         and patch.distance(Point(s.xy)) < .1]
            if len(witnesses) < 2 or patch.intersects(obstacles.water) or any(patch.intersects(line) for line in obstacles.unresolved_lines):
                continue
            touching = [p for p in shell.geoms if patch.boundary.intersection(p.boundary.buffer(.05)).length > 1]
            # A nearby fragment split off by an invalid vertex may reconnect
            # through valid local witnesses; distant detached islands may not.
            if len(touching) > 1 and any(p.distance(q) > 2 * max(config.interior_gap_radii_m, default=0)
                                        for p in touching for q in touching):
                continue
            additions.append(patch)
            repairs.append(dict(source='valid_witness_local_mesh', area_m2=patch.area,
                                attachment_fraction=attachment, unsupported_fraction=unsupported,
                                evidence_point_ids=witnesses, xy=list(patch.representative_point().coords)[0]))
    # Work on each existing component separately: never connect detached islands.
    if repair_gaps:
        for poly in shell.geoms:
            for radius in config.interior_gap_radii_m:
                closed = poly.buffer(radius, join_style=2).buffer(-radius, join_style=2)
                for patch in multipolygon(closed.difference(poly)).geoms:
                    if patch.area < .01:
                        continue
                    attachment = patch.boundary.intersection(poly.boundary.buffer(.05)).length / patch.length
                    unsupported = patch.intersection(built.unknown).area / patch.area
                    row = dict(source='bounded_closing', radius_m=radius, area_m2=patch.area, attachment_fraction=attachment,
                               unsupported_fraction=unsupported, xy=list(patch.representative_point().coords)[0])
                    # Only two-sided, unsupported concavities are filled. A known
                    # negative in an open notch still requires boundary evidence.
                    negative = any(s.evidence.reachable is False and patch.covers(Point(s.xy)) for s in samples)
                    if attachment < .55 or unsupported < .5 or patch.area > config.max_triangle_edge_m ** 2 / 4:
                        continue
                    if sum(patch.boundary.intersection(p.boundary.buffer(.05)).length > 1 for p in shell.geoms) != 1:
                        continue
                    if patch.intersects(obstacles.water) or negative or any(patch.intersects(line) for line in obstacles.unresolved_lines):
                        row['reason'] = 'hard_obstacle_or_valid_time_conflict'
                        unresolved.append(row)
                        continue
                    additions.append(patch)
                    repairs.append({**row, 'source': 'inward_notch_repair'})
    shell = filled_shells(unary_union([shell, *additions])).intersection(built.extent)
    mask, corridors, bridges = obstacles.mask(samples, shell, config.bridge_display_width_m)
    geometry = multipolygon(shell.difference(mask))
    # Do not keep asking about a candidate already repaired by the independent
    # valid-witness proposal pass.
    unresolved = [r for r in unresolved if not geometry.covers(Point(r['xy']))]
    inferred = multipolygon(geometry.difference(built.geometry))
    conflicts = []
    for s in samples:
        p = Point(s.xy)
        if s.evidence.reachable is False and inferred.covers(p):
            conflicts.append(dict(point_id=s.point_id, xy=s.xy, duration_s=s.evidence.duration,
                                  origin_offset_m=s.evidence.origin_offset_m,
                                  destination_offset_m=s.evidence.destination_offset_m,
                                  distance_to_exterior_m=shell.boundary.distance(p),
                                  source='continuous_interior_fill'))
    if not geometry.is_valid:
        raise ValueError('invalid_continuous_coverage_geometry')
    new_samples = [s for s in samples if geometry.covers(Point(s.xy))
                   and not built.geometry.covers(Point(s.xy))]
    stats = dict(reachable=sum(s.evidence.reachable is True for s in new_samples),
                 unreachable=sum(s.evidence.reachable is False for s in new_samples),
                 unknown=sum(s.evidence.reachable is None for s in new_samples),
                 scope='algorithm_samples_newly_included_by_fill_not_independent_validation')
    return Coverage(geometry, shell, inferred, mask, corridors, repairs, unresolved, conflicts, bridges, stats)
