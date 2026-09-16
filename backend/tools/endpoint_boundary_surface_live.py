"""One explicitly enabled E6.1 2.4 km run, with a durable 800-attempt ceiling."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET

import httpx

from app.analyses import LimitedProvider, RateGate
from app.baidu import silence_transport_logs
from app.config import Settings
from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import CancelToken, IsochroneRequest
from life_circle.providers import BaiduProvider
from tools.endpoint_boundary_surface import compute_boundary_surface
from tools.endpoint_live import EndpointTransport
from tools.live_smoke import Ledger, LiveGuardError, ORIGIN, dump
from tools.qps_review import metrics

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path('D:/CodexOutputs/guodingyi-boundary-surface-e61-2400')
EXTENT = 1200  # Half-width in metres; the complete square is 2400 x 2400 m.


def make_request():
    return IsochroneRequest(ORIGIN, 'bd09ll', budget=800, qps=3, concurrency=1,
                            extent=EXTENT, max_extent=EXTENT, expand=False)


class SurfaceLedger(Ledger):
    phase_limits = {'analysis': 800}
    total_limit = 800

    def __init__(self, root):
        super().__init__(root)
        self.token = CancelToken()
        self.deadline = float('inf')

    def arm(self):
        if self.data['analysis_started'] or sum(self.data['counts'].values()):
            raise LiveGuardError('single_run_already_started_no_reset')
        self.data['analysis_started'] = True
        self.save()

    def reserve(self, phase, request, monotonic, **kwargs):
        if not self.data['analysis_started'] or self.token.cancelled or time.monotonic() >= self.deadline:
            raise LiveGuardError('unarmed_cancelled_or_deadline')
        try:
            lat, lng = map(float, request.url.params['origin'].split(','))
            dlat, dlng = map(float, request.url.params['destination'].split(','))
            if normalize((lng, lat)) != ORIGIN:
                raise ValueError
            # Allow only the sub-decimetre round-trip error from six-decimal coordinates.
            if any(abs(x) > EXTENT + .1 for x in LocalProjection(ORIGIN).to_local((dlng, dlat))):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise LiveGuardError('outside_fixed_request_domain') from None
        return super().reserve(phase, request, monotonic, **kwargs)

    def save(self):
        try:
            super().save()
        except OSError:
            self.data['halted'] = 'ledger_write_failed'
            self.token.cancel()
            raise LiveGuardError('ledger_write_failed') from None


def public_result(result):
    return {k:v for k,v in result.items() if not k.startswith('_')}


def freeze():
    tests = OUTPUT/'full-final.xml'
    suites = ET.parse(tests).getroot()
    if not list(suites.iter('testcase')) or any(int(s.get('failures', 0))+int(s.get('errors', 0)) for s in suites.iter('testsuite')):
        raise LiveGuardError('offline_regression_not_passed')
    paths = sorted([*ROOT.joinpath('backend').rglob('*.py'),
                    *ROOT.joinpath('life-circle-algorithm/src').rglob('*.py')])
    return dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        source_sha256={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        dependencies={name:importlib.metadata.version(name) for name in ('numpy','shapely','contourpy','httpx','pytest')},
        tests_sha256=hashlib.sha256(tests.read_bytes()).hexdigest(),
        config=dict(origin=ORIGIN, coordinateSystem='bd09ll', thresholdSeconds=900,
                    budget=800, qps=3, concurrency=1, timeout=8, max_attempts=2,
                    deadlineSeconds=600, extent=EXTENT, max_extent=EXTENT,
                    coarse_size=400, min_size=50, expand=False, seed=20260911),
        started_utc=datetime.now(timezone.utc).isoformat())


async def run(ledger, settings):
    transport = EndpointTransport(ledger, httpx.AsyncHTTPTransport(retries=0, trust_env=False))
    transport.phase = 'analysis'
    request = make_request()
    ledger.deadline = time.monotonic()+600
    last = [-1]
    def progress(value):
        if value['requests']//20 != last[0]:
            last[0] = value['requests']//20
            dump(ledger.root/'progress.json', value)
            print(json.dumps(value), flush=True)
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
        provider = LimitedProvider(BaiduProvider(settings.baidu_map_ak.get_secret_value(), client=client), RateGate(3))
        result = await compute_boundary_surface(request, provider, ledger.token, on_progress=progress)
    dump(ledger.root/'observations.json', [asdict(o) for o in result['_observations']])
    dump(ledger.root/'evidence.json', result['_evidence'])
    dump(ledger.root/'brackets.json', result['_brackets'])
    dump(ledger.root/'result.json', public_result(result))
    measured = metrics(ledger.data['events'])
    summary = dict(metrics=measured, reserved=ledger.data['counts']['analysis'],
                   quality=result['quality'], status=result['status'], stopReason=result['stopReason'],
                   halted=ledger.data['halted'], boundaryModel=result['boundaryModel'],
                   elapsedSeconds=result['elapsedSeconds'], finished_utc=datetime.now(timezone.utc).isoformat())
    dump(ledger.root/'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live', required=True, action='store_true')
    parser.add_argument('--env-file', required=True, type=Path)
    args = parser.parse_args()
    silence_transport_logs()
    try:
        settings = Settings(_env_file=args.env_file)
        if not settings.ak_configured or settings.analysis_provider != 'baidu' or settings.analysis_qps != 3:
            raise LiveGuardError('invalid_real_provider_configuration')
        frozen = freeze()
        with SurfaceLedger(OUTPUT/'run-01') as ledger:
            ledger.arm()
            dump(ledger.root/'frozen.json', frozen)
            asyncio.run(run(ledger, settings))
    except Exception as error:
        # Exception messages can contain HTTP URLs or configuration secrets.
        print(json.dumps(dict(status='stopped', error_type=type(error).__name__)), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
