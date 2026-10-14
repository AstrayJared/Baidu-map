"""Portable offline runtime packages. Standard library only; never downloads data."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from datetime import datetime, timezone
import zipfile

REPO = Path(__file__).resolve().parents[2]
OSM_KEYS = {'OSM_REGION_ID', 'OSM_PBF_PATH', 'OSM_GRAPH_CACHE_PATH', 'OSM_DATA_VERSION',
            'OSM_METRIC_CRS', 'WALK_SPEED_MPS', 'OSM_COVERAGE_BOUNDARY_PATH',
            'HYBRID_RISK_PATH', 'HYBRID_OBSTACLE_PATH', 'WATER_REVIEW_DIR'}
ROLES = {'graph': 'OSM_GRAPH_CACHE_PATH', 'coverage': 'OSM_COVERAGE_BOUNDARY_PATH',
         'risk': 'HYBRID_RISK_PATH', 'obstacle': 'HYBRID_OBSTACLE_PATH'}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def header(path, graph=False):
    opener = gzip.open if graph else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        prefix = stream.read(512 * 1024)
    if graph:
        if not re.search(r'"cache_version"\s*:\s*1\s*[,}]', prefix):
            raise ValueError('unsupported graph cache format')
        position = prefix.index('"metadata"') + len('"metadata"')
        return json.JSONDecoder().raw_decode(prefix[position:].lstrip().removeprefix(':').lstrip())[0]
    return json.loads(prefix[:prefix.index('"features"')].rstrip().rstrip(',') + '}')


def member(root, relative):
    p = PurePosixPath(relative)
    if p.is_absolute() or not p.parts or any(x in ('..', '.') or ':' in x or '\\' in x for x in p.parts):
        raise ValueError('unsafe package path')
    result = (Path(root) / Path(*p.parts)).resolve()
    if not result.is_relative_to(Path(root).resolve()):
        raise ValueError('package path outside root')
    return result


def verify(directory):
    root = Path(directory).resolve()
    manifest = json.loads((root / 'manifest.json').read_text('utf-8'))
    if manifest.get('schemaVersion') != 1 or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', manifest.get('regionId', '')):
        raise ValueError('invalid package manifest')
    if not manifest.get('source') or not re.fullmatch(r'[0-9a-f]{64}', manifest.get('sourcePbfSha256', '')):
        raise ValueError('package provenance missing')
    if not manifest.get('dataVersion') or not manifest.get('metricCrs') or not manifest.get('walkingSpeedMps', 0) > 0:
        raise ValueError('package runtime metadata missing')
    files = manifest['files']
    if not set(ROLES) <= set(files) or 'metadata' not in files:
        raise ValueError('package is missing a required runtime role')
    for role, item in files.items():
        p = member(root, item['path'])
        if not p.is_file() or p.is_symlink() or sha256(p) != item['sha256']:
            raise ValueError('package checksum mismatch: ' + role)
    metadata = json.loads(member(root, files['metadata']['path']).read_text('utf-8'))
    graph = header(member(root, files['graph']['path']), graph=True)
    for value in (metadata, graph):
        if value.get('osm_data_version') != manifest['dataVersion'] or value.get('crs') != manifest['metricCrs']:
            raise ValueError('graph metadata version/CRS mismatch')
        if value.get('walking_speed_mps') != manifest['walkingSpeedMps']:
            raise ValueError('graph walking speed mismatch')
    if graph.get('coverage_sha256') != files['coverage']['sha256']:
        raise ValueError('coverage does not match graph snapshot')
    if metadata.get('pbf_sha256') != manifest['sourcePbfSha256']:
        raise ValueError('source snapshot mismatch')
    for role in ('risk', 'obstacle'):
        side = header(member(root, files[role]['path']))
        if side.get('osm_data_version') != manifest['dataVersion'] or side.get('coordinate_system') != 'wgs84':
            raise ValueError(role + ' version/coordinate mismatch')
        if role == 'obstacle' and side.get('source_pbf_sha256') != manifest['sourcePbfSha256']:
            raise ValueError('obstacle source snapshot mismatch')
    for role, item in files.items():
        if role.startswith('review:'):
            review = json.loads(member(root, item['path']).read_text('utf-8'))
            applies = review.get('appliesTo') or {}
            if applies.get('osmDataVersion') != manifest['dataVersion']:
                raise ValueError('review version mismatch')
            if applies.get('sourcePbfSha256') not in (None, manifest['sourcePbfSha256']):
                raise ValueError('review source snapshot mismatch')
    return manifest


def write_env(path, updates):
    """Preserve comments, unknown keys and credentials; replace only OSM keys."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text('utf-8') if path.exists() else ''
    lines, seen = [], set()
    for line in original.splitlines():
        name = line.partition('=')[0].strip()
        if name in updates and not line.lstrip().startswith('#'):
            if name not in seen:
                lines.append(name + '=' + updates[name])
                seen.add(name)
        else:
            lines.append(line)
    lines.extend(k + '=' + v for k, v in updates.items() if k not in seen)
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        shutil.copy2(path, path.with_name(path.name + '.osm-backup-' + stamp))
    temporary = path.with_name(path.name + '.osm-tmp')
    temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def activate(root, manifest, env_file):
    env_file = Path(env_file).resolve()
    updates = {'OSM_REGION_ID': manifest['regionId'], 'OSM_DATA_VERSION': manifest['dataVersion'],
               'OSM_METRIC_CRS': manifest['metricCrs'], 'WALK_SPEED_MPS': str(manifest['walkingSpeedMps']),
               'OSM_PBF_PATH': '', 'WATER_REVIEW_DIR': ''}
    for role, key in ROLES.items():
        updates[key] = json.dumps(str(member(root, manifest['files'][role]['path'])))
    reviews = [item for role, item in manifest['files'].items() if role.startswith('review:')]
    if reviews:
        updates['WATER_REVIEW_DIR'] = json.dumps(str(member(root, reviews[0]['path']).parent))
    write_env(env_file, updates)


def import_package(archive, *, env_file=None, data_root=None):
    archive = Path(archive).resolve()
    data_root = Path(data_root or REPO / 'data/osm/regions').resolve()
    env_file = Path(env_file or REPO / 'backend/.env').resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    checksum = archive.with_name(archive.name + '.sha256')
    checksum_parts = checksum.read_text('utf-8').split() if checksum.is_file() else []
    if not checksum_parts or checksum_parts[0] != sha256(archive):
        raise ValueError('archive checksum missing or mismatched')
    with tempfile.TemporaryDirectory(prefix='.import-', dir=data_root) as temporary:
        with zipfile.ZipFile(archive) as bundle:
            if sum(i.file_size for i in bundle.infolist()) > 3 * 1024**3:
                raise ValueError('package exceeds extraction limit')
            for item in bundle.infolist():
                target = member(temporary, item.filename)
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('package contains a symbolic link')
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(item) as source, target.open('wb') as output:
                        shutil.copyfileobj(source, output)
        manifest = verify(temporary)
        destination = data_root / (manifest['regionId'] + '-' + sha256(archive)[:12])
        if not destination.exists():
            shutil.copytree(temporary, destination)
        verify(destination)
        activate(destination, manifest, env_file)
    return destination


def pack(source, output, region_id='shanghai-20260912'):
    source, output = Path(source).resolve(), Path(output).resolve()
    metadata = json.loads((source / 'graph_metadata.json').read_text('utf-8'))
    names = {'graph': 'shanghai.osm-cache', 'metadata': 'graph_metadata.json',
             'coverage': 'shanghai.poly', 'risk': 'shanghai.risks.geojson',
             'obstacle': 'shanghai.obstacles.geojson'}
    manifest = {'schemaVersion': 1, 'regionId': region_id, 'dataVersion': metadata['osm_data_version'],
        'metricCrs': metadata['crs'], 'walkingSpeedMps': metadata['walking_speed_mps'],
        'source': metadata['source'], 'sourcePbfSha256': metadata['pbf_sha256'],
        'attribution': '© OpenStreetMap contributors; ODbL',
        'attributionUrl': 'https://www.openstreetmap.org/copyright',
        'license': 'ODbL-1.0', 'licenseUrl': 'https://opendatacommons.org/licenses/odbl/1-0/',
        'files': {}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='osm-pack-') as temporary:
        for role, name in names.items():
            target = Path(temporary) / name
            shutil.copy2(source / name, target)
            if role == 'metadata':
                sanitized = {k: v for k, v in metadata.items() if not k.endswith('_path')}
                target.write_text(json.dumps(sanitized, ensure_ascii=False, indent=2), encoding='utf-8')
            manifest['files'][role] = {'path': name, 'sha256': sha256(target)}
        reviews = source / 'reviews'
        if reviews.exists():
            for p in sorted(reviews.glob('*.json')):
                review = json.loads(p.read_text('utf-8'))
                applies = review.get('appliesTo') or {}
                if (applies.get('osmDataVersion') == manifest['dataVersion']
                        and applies.get('sourcePbfSha256') in (None, manifest['sourcePbfSha256'])):
                    target = Path(temporary) / 'reviews' / p.name
                    target.parent.mkdir(exist_ok=True)
                    shutil.copy2(p, target)
                    manifest['files']['review:' + p.stem] = {'path': 'reviews/' + p.name, 'sha256': sha256(target)}
        (Path(temporary) / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        verify(temporary)
        staging = output.with_name(output.name + '.tmp')
        with zipfile.ZipFile(staging, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for p in sorted(Path(temporary).rglob('*')):
                if p.is_file():
                    bundle.write(p, p.relative_to(temporary).as_posix())
        os.replace(staging, output)
    output.with_name(output.name + '.sha256').write_text(sha256(output) + '  ' + output.name + '\n', encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('pack'); p.add_argument('--source', type=Path, required=True); p.add_argument('--output', type=Path, required=True)
    p.add_argument('--region', default='shanghai-20260912')
    p = commands.add_parser('import'); p.add_argument('archive', type=Path); p.add_argument('--env-file', type=Path); p.add_argument('--data-root', type=Path)
    p = commands.add_parser('verify'); p.add_argument('directory', type=Path)
    p = commands.add_parser('clear'); p.add_argument('--env-file', type=Path, default=REPO / 'backend/.env')
    args = parser.parse_args()
    try:
        if args.command == 'pack':
            result = pack(args.source, args.output, args.region)
        elif args.command == 'import':
            result = {'installed': str(import_package(args.archive, env_file=args.env_file, data_root=args.data_root))}
        elif args.command == 'verify':
            result = verify(args.directory)
        else:
            write_env(args.env_file, {k: ('unconfigured' if k == 'OSM_DATA_VERSION' else 'EPSG:3857' if k == 'OSM_METRIC_CRS' else '1.3' if k == 'WALK_SPEED_MPS' else '') for k in sorted(OSM_KEYS)})
            result = {'cleared': True}
        print(json.dumps(result, ensure_ascii=True))
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        parser.exit(1, 'OSM package error: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
