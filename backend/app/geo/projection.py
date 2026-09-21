from pyproj import CRS, Transformer
from shapely import make_valid, segmentize
from shapely.geometry import mapping
from shapely.ops import transform

from .coordinates import bd09_to_wgs84, wgs84_to_bd09


class MetricProjection:
    def __init__(self, crs):
        self.crs = CRS.from_user_input(crs)
        if not self.crs.is_projected or any(a.unit_conversion_factor != 1 for a in self.crs.axis_info[:2]):
            raise ValueError("metric_crs_must_use_metres")
        self.forward = Transformer.from_crs(4326, self.crs, always_xy=True)
        self.inverse = Transformer.from_crs(self.crs, 4326, always_xy=True)

    def origin(self, bd09):
        return self.forward.transform(*bd09_to_wgs84(*bd09), errcheck=True)

    def public_geometry(self, geometry, *, repair_roundoff=False):
        if repair_roundoff and not geometry.is_valid:
            raise ValueError('invalid_source_geometry')
        # Shapely falls back to scalar callbacks for this scalar converter.
        def convert(x, y, z=None):
            return wgs84_to_bd09(*self.inverse.transform(x, y, errcheck=True))
        result = transform(convert, geometry)
        # A nonlinear BD09 transform bends metric straight lines. Long public
        # chords can cross an adjacent ring even when the metric source is
        # valid. Densify in metres before considering a tiny roundoff repair;
        # never loosen the area-change tolerance to hide these crossings.
        attempts = (None, 10, 2, .5) if repair_roundoff else (None,)
        for spacing in attempts:
            if spacing is not None:
                result = transform(convert, segmentize(geometry, spacing))
            if result.is_valid:
                break
            if not repair_roundoff:
                continue
            repaired = make_valid(result)
            # Only permit negligible area changes from boundary roundoff.
            # Larger topology changes must still fail instead of hiding errors.
            if abs(repaired.area - result.area) <= max(1e-16, abs(result.area) * 1e-8):
                if geometry.geom_type in ('Polygon', 'MultiPolygon') and repaired.geom_type not in ('Polygon', 'MultiPolygon'):
                    from shapely.geometry import MultiPolygon
                    polygons = []
                    for part in getattr(repaired, 'geoms', []):
                        if part.geom_type == 'Polygon':
                            polygons.append(part)
                        elif part.geom_type == 'MultiPolygon':
                            polygons.extend(part.geoms)
                    repaired = MultiPolygon(polygons)
                result = repaired
                if result.is_valid:
                    break
        if not result.is_valid:
            raise ValueError("public_geometry_invalid")
        return {**mapping(result), "coordinateSystem": "bd09ll"}
