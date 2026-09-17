"""One metric domain for proposals, normalized requests and public geometry."""
import math

from shapely.geometry import box

ALGORITHM_VERSION = "hybrid-v1.5.0"
LEDGER_VERSION = "hybrid-v1.5"
# BD09 requests have six decimals. Propose edge witnesses slightly inside the
# domain; the exact normalized coordinate still goes through contains().
EDGE_INSET_M = 0.5


def computation_extent(origin, config):
    x, y = origin
    half = config.analysis_half_width_m
    return box(x - half, y - half, x + half, y + half)


def contains(origin, xy, config):
    dx, dy = xy[0] - origin[0], xy[1] - origin[1]
    return (all(math.isfinite(v) for v in (dx, dy))
            and max(abs(dx), abs(dy)) <= config.analysis_half_width_m
            and math.hypot(dx, dy) <= config.max_exploration_radius_m)


def ray_limit(angle, config):
    half = max(0, config.analysis_half_width_m - EDGE_INSET_M)
    return min(half / max(abs(math.cos(angle)), abs(math.sin(angle))),
               max(0, config.max_exploration_radius_m - EDGE_INSET_M))
