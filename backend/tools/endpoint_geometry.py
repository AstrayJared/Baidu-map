"""Validated research export; only micrometre-scale projection roundoff is repaired.

Reject invalid local construction. A valid local polygon can acquire intersecting
sliver rings on translation to geographic floats. Repairs are permitted only when
their occupied set and area agree with the valid source within explicit bounds.
Lower-dimensional remnants are diagnostics, not polygonal reachability evidence.
"""
import numpy as np
from shapely import make_valid, union_all, difference, intersection
from shapely.geometry import mapping, shape
from shapely.ops import transform

from life_circle.field import GeometryError, business_geometry as raw_geometry, multipolygon

ROUNDTRIP_TOLERANCE_M = 1e-6
# Stabilize polygon overlays BEFORE export. Never quantize measured endpoints,
# durations, query positions or bracket widths. This grid is 10x finer than the
# unchanged export tolerance; output still undergoes the same evidence checks.
OVERLAY_GRID_M = 1e-7


def polygon_union(parts):
    return multipolygon(union_all(parts, grid_size=OVERLAY_GRID_M))


def polygon_difference(left, right):
    return multipolygon(difference(left, right, grid_size=OVERLAY_GRID_M))


def polygon_intersection(left, right):
    return multipolygon(intersection(left, right, grid_size=OVERLAY_GRID_M))


def business_geometry(geometry, projection):
    original = multipolygon(geometry)
    if not original.is_valid:
        raise GeometryError('Invalid local research geometry')
    result = raw_geometry(original, projection)
    exported = shape(result)
    if exported.is_valid:
        return result
    repaired = multipolygon(make_valid(exported))
    restored = transform(lambda x, y, z=None: (
        (np.asarray(x) - projection.origin[0]) * projection.sx,
        (np.asarray(y) - projection.origin[1]) * projection.sy), repaired)
    restored = multipolygon(make_valid(restored))
    tolerance = ROUNDTRIP_TOLERANCE_M
    # GEOS Hausdorff samples ring vertices and penalizes removal of zero-width
    # hole spikes. Check mutual occupied-set containment instead; report topology
    # changes separately in the audit, never as improved boundary accuracy.
    if (not repaired.is_valid or repaired.is_empty != original.is_empty
            or not original.difference(restored.buffer(tolerance)).is_empty
            or not restored.difference(original.buffer(tolerance)).is_empty
            or original.symmetric_difference(restored).area > max(1e-8, original.length * tolerance)):
        raise GeometryError('Projection repair exceeds local research tolerance')
    return dict(mapping(repaired), coordinateSystem='bd09ll')
