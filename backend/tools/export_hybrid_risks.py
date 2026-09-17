"""Offline PBF -> WGS84 risk sidecar. No route API calls."""
import argparse
import json
from pathlib import Path

from app.persistence import atomic_dump


def export(pbf, output, version):
    from pyrosm import OSM
    data = OSM(str(pbf)).get_data_by_custom_criteria(
        custom_filter={"natural": ["water"], "waterway": ["river", "canal", "riverbank"],
                       "railway": ["rail", "light_rail"], "barrier": True, "access": ["private", "no"],
                       "highway": ["motorway", "trunk", "motorway_link", "trunk_link"]},
        tags_as_columns=["natural", "waterway", "railway", "barrier", "access", "highway"],
        keep_nodes=True, keep_ways=True, keep_relations=True)
    features = []
    if data is not None:
        for feature in json.loads(data.to_crs(4326).to_json())["features"]:
            p = feature["properties"]
            kind = "water" if p.get("natural") == "water" or p.get("waterway") else "railway" if p.get("railway") else "barrier" if p.get("barrier") not in (None, "no") else "major_road" if p.get("highway") else "restricted_access"
            features.append({"type": "Feature", "geometry": feature["geometry"],
                             "properties": {"risk_kind": kind, "osm_id": p.get("id")}})
    atomic_dump(output, {"type": "FeatureCollection", "coordinate_system": "wgs84",
                        "osm_data_version": version, "features": features})
    return len(features)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pbf", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    print({"risk_features": export(args.pbf, args.output, args.version)})
