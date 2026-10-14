import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


@pytest.mark.parametrize('source,model', [('gcj02', 1), ('wgs84', 2)])
def test_official_model_and_independent_ledger(tmp_path, source, model):
    calls = []
    def respond(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json={'status': 0, 'result': [{'x': 121.5, 'y': 31.3}]})
    app = create_app(Settings(_env_file=None, analysis_provider='baidu', baidu_map_ak='secret',
        quota_ledger_path=tmp_path/'quota.db', checkup_dir=tmp_path/'checkups'),
        coordinate_transport=httpx.MockTransport(respond))
    with TestClient(app) as client:
        response = client.post('/api/v2/coordinates/convert', json={
            'point': {'lng': 121.49, 'lat': 31.29}, 'sourceCoordinateSystem': source})
        assert response.status_code == 200
        assert response.json()['point'] == {'lng': 121.5, 'lat': 31.3}
        assert calls[0]['model'] == str(model)
        assert app.state.quota.ledger.spent('geoconv') == 1
        assert app.state.quota.ledger.spent('place') == 0
        assert 'secret' not in response.text
        result = client.post('/api/v2/coordinates/convert', json={
            'point': {'lng': 121.49, 'lat': 31.29}, 'sourceCoordinateSystem': 'bd09ll'})
        assert result.json()['provider'] == 'identity' and len(calls) == 1


@pytest.mark.parametrize('body,expected,code', [
    ({'status': 101}, 502, 'coordinates_permission_denied'),
    ({'status': 302}, 429, 'coordinates_upstream_quota'),
    ({'status': 401}, 429, 'coordinates_rate_limited'),
    ({'status': 4}, 502, 'coordinates_conversion_failed'),
    ({'status': False, 'result': [{'x': 121, 'y': 31}]}, 502, 'coordinates_invalid_response'),
    ({'status': 0, 'result': []}, 502, 'coordinates_invalid_response'),
    ({'status': 0, 'result': [{'x': '121', 'y': True}]}, 502, 'coordinates_invalid_response')])
def test_bad_upstream_never_falls_back_or_leaks(tmp_path, body, expected, code):
    app = create_app(Settings(_env_file=None, baidu_map_ak='secret',
        quota_ledger_path=tmp_path/'quota.db', checkup_dir=tmp_path/'checkups'),
        coordinate_transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    with TestClient(app) as client:
        r = client.post('/api/v2/coordinates/convert', json={
            'point': {'lng': 121., 'lat': 31.}, 'sourceCoordinateSystem': 'wgs84'})
        assert r.status_code == expected and r.json()['code'] == code
        assert 'point' not in r.json() and 'secret' not in r.text


def test_synthetic_and_exhausted_services_never_dispatch(tmp_path):
    def forbidden(_):
        pytest.fail('unexpected external conversion')
    for provider, cap, expected in [('synthetic', 100, 503), ('baidu', 0, 429)]:
        app = create_app(Settings(_env_file=None, baidu_map_ak='secret', analysis_provider=provider,
            baidu_geoconv_daily_budget=cap, quota_ledger_path=tmp_path/(provider+'.db'),
            checkup_dir=tmp_path/provider), coordinate_transport=httpx.MockTransport(forbidden))
        with TestClient(app) as client:
            r = client.post('/api/v2/coordinates/convert', json={
                'point': {'lng': 121., 'lat': 31.}, 'sourceCoordinateSystem': 'gcj02'})
            assert r.status_code == expected


@pytest.mark.parametrize('upstream,expected,code', [(429,429,'coordinates_rate_limited'),
    (403,502,'coordinates_permission_denied'), ('timeout',504,'coordinates_timeout')])
def test_named_transport_failures_preserve_a_separate_attempt(tmp_path, upstream, expected, code):
    def respond(request):
        if upstream == 'timeout':
            raise httpx.ReadTimeout('private upstream details', request=request)
        return httpx.Response(upstream, text='private upstream details')
    app = create_app(Settings(_env_file=None, baidu_map_ak='secret',
        quota_ledger_path=tmp_path/'quota.db', checkup_dir=tmp_path/'checkups'),
        coordinate_transport=httpx.MockTransport(respond))
    with TestClient(app) as client:
        r = client.post('/api/v2/coordinates/convert', json={
            'point':{'lng':121.,'lat':31.},'sourceCoordinateSystem':'gcj02'})
        assert r.status_code == expected and r.json()['code'] == code
        assert 'point' not in r.json() and 'private upstream' not in r.text
        assert app.state.quota.ledger.spent('geoconv') == 1
