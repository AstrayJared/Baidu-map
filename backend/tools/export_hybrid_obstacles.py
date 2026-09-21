"""Offline water/bridge sidecar retaining source tags; never calls a route API."""
import argparse
import hashlib
import json
from pathlib import Path

from app.persistence import atomic_dump

TAGS = ['natural', 'water', 'waterway', 'width', 'bridge', 'tunnel', 'layer',
        'access', 'foot', 'highway', 'covered', 'location', 'name']


def export(pbf, output, version):
    from pyrosm import OSM
    data = OSM(str(pbf)).get_data_by_custom_criteria(
        custom_filter={'natural': ['water'], 'waterway': ['river', 'canal', 'riverbank', 'stream'],
                       'bridge': True}, tags_as_columns=TAGS,
        keep_nodes=False, keep_ways=True, keep_relations=True)
    features = []
    if data is not None:
        for f in json.loads(data.to_crs(4326).to_json())["features"]:
            p = f['properties']
            kind = 'water' if p.get('natural') == 'water' or p.get('waterway') else 'bridge'
            props = {k: p.get(k) for k in TAGS if p.get(k) is not None}
            props.update(kind=kind, osm_id=p.get('id'), osm_type=p.get('osm_type'))
            features.append(dict(type='Feature', geometry=f['geometry'], properties=props))
    payload = dict(type='FeatureCollection', schema_version=1, coordinate_system='wgs84',
                   osm_data_version=version, source_pbf_sha256=hashlib.sha256(pbf.read_bytes()).hexdigest(),
                   features=features)
    atomic_dump(output, payload)
    print(json.dumps(dict(features=len(features), output=str(output))), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pbf', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    export(args.pbf, args.output, args.version)
