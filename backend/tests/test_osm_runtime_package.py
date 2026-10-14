import gzip
import json
from pathlib import Path
import zipfile
import pytest
from scripts.setup_osm_region import sha256, pack, import_package, verify, write_env


def source(root):
    root.mkdir()
    (root/'shanghai.poly').write_text('test polygon', encoding='utf-8')
    metadata = {'osm_data_version': 'test-v1', 'crs': 'EPSG:32651', 'walking_speed_mps': 1.3,
        'source': 'https://example.org/test.osm.pbf', 'pbf_sha256': 'a'*64,
        'coverage_sha256': sha256(root/'shanghai.poly'), 'cache_path': 'private-local-path'}
    (root/'graph_metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
    with gzip.open(root/'shanghai.osm-cache', 'wt', encoding='utf-8') as f:
        json.dump({'cache_version': 1, 'metadata': metadata, 'nodes': [], 'edges': []}, f)
    for name in ('risk', 'obstacle'):
        data = {'type': 'FeatureCollection', 'osm_data_version': 'test-v1',
                'coordinate_system': 'wgs84', 'source_pbf_sha256': 'a'*64, 'features': []}
        (root/('shanghai.risks.geojson' if name == 'risk' else 'shanghai.obstacles.geojson')).write_text(json.dumps(data), encoding='utf-8')
    return root


def test_portable_package_preserves_other_config_and_checks_versions(tmp_path):
    archive = tmp_path/'sample.zip'
    pack(source(tmp_path/'source'), archive, 'test-region')
    env = tmp_path/'new space'/'backend'/'.env'
    env.parent.mkdir(parents=True)
    env.write_text('# preserve\nBAIDU_MAP_AK=unchanged-secret\nANALYSIS_QPS=3\n', encoding='utf-8')
    destination = import_package(archive, env_file=env, data_root=tmp_path/'new space'/'regions')
    assert verify(destination)['dataVersion'] == 'test-v1'
    text = env.read_text('utf-8')
    assert 'BAIDU_MAP_AK=unchanged-secret' in text and 'ANALYSIS_QPS=3' in text
    assert list(env.parent.glob('.env.osm-backup-*'))
    assert 'private-local-path' not in (destination/'graph_metadata.json').read_text('utf-8')
    (destination/'shanghai.risks.geojson').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='checksum'):
        verify(destination)


def test_bad_zip_never_changes_env_or_escapes(tmp_path):
    archive = tmp_path/'bad.zip'
    with zipfile.ZipFile(archive, 'w') as f:
        f.writestr('../escaped', 'bad')
    archive.with_name(archive.name+'.sha256').write_text(sha256(archive), encoding='utf-8')
    env = tmp_path/'.env'; env.write_text('BAIDU_MAP_AK=keep', encoding='utf-8')
    with pytest.raises(ValueError, match='unsafe'):
        import_package(archive, env_file=env, data_root=tmp_path/'regions')
    assert env.read_text('utf-8') == 'BAIDU_MAP_AK=keep' and not (tmp_path/'escaped').exists()


def test_pack_rejects_mismatched_source_before_activation(tmp_path):
    root = source(tmp_path/'source')
    p = root/'shanghai.obstacles.geojson'
    value = json.loads(p.read_text('utf-8')); value['osm_data_version'] = 'other'
    p.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='version'):
        pack(root, tmp_path/'bad.zip')


def test_applicable_reviews_are_portable_and_mismatched_reviews_are_excluded(tmp_path):
    root = source(tmp_path/'source')
    reviews = root/'reviews'; reviews.mkdir()
    for name, version in [('current', 'test-v1'), ('old', 'other')]:
        (reviews/(name+'.json')).write_text(json.dumps({'schemaVersion': 1,
            'appliesTo': {'osmDataVersion': version, 'sourcePbfSha256': 'a'*64}}), encoding='utf-8')
    archive = tmp_path/'sample.zip'
    manifest = pack(root, archive, 'test-region')
    assert 'review:current' in manifest['files'] and 'review:old' not in manifest['files']
    env = tmp_path/'.env'
    destination = import_package(archive, env_file=env, data_root=tmp_path/'regions')
    assert (destination/'reviews/current.json').is_file()
    assert 'WATER_REVIEW_DIR=' in env.read_text('utf-8')


@pytest.mark.parametrize('damage', ['missing-role', 'unsupported-format', 'empty-checksum'])
def test_invalid_package_keeps_configuration_unchanged(tmp_path, damage):
    archive = tmp_path/'sample.zip'
    pack(source(tmp_path/'source'), archive, 'test-region')
    env = tmp_path/'.env'; env.write_text('BAIDU_MAP_AK=preserve\nOSM_DATA_VERSION=previous\n', encoding='utf-8')
    before = env.read_bytes()
    if damage == 'empty-checksum':
        archive.with_name(archive.name+'.sha256').write_text('', encoding='utf-8')
    else:
        with zipfile.ZipFile(archive) as bundle:
            members = {name: bundle.read(name) for name in bundle.namelist()}
        manifest = json.loads(members['manifest.json'])
        if damage == 'missing-role':
            del manifest['files']['obstacle']
        else:
            manifest['schemaVersion'] = 999
        members['manifest.json'] = json.dumps(manifest).encode()
        with zipfile.ZipFile(archive, 'w') as bundle:
            for name, value in members.items(): bundle.writestr(name, value)
        archive.with_name(archive.name+'.sha256').write_text(sha256(archive), encoding='utf-8')
    with pytest.raises(ValueError):
        import_package(archive, env_file=env, data_root=tmp_path/'regions')
    assert env.read_bytes() == before
