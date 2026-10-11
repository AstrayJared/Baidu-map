import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.poi.models import CollectionConfig, PoiCollectRequest, RuntimeConfig
from app.poi.planner import build_plan
from app.poi.runtime import PoiLedger, PoiRuntime
from app.request_control import RequestStopped


def config(phase='collect', **overrides):
    request = PoiCollectRequest(coordinateSystem='bd09ll')
    runtime = RuntimeConfig(runId='runtime-test', phase=phase, **overrides)
    return request, runtime, build_plan(request, runtime)


def test_live_runtime_fails_closed_before_provider_use():
    _, runtime_config, plan = config(authorized=False)
    runtime = PoiRuntime(runtime_config, plan, live=True)
    with pytest.raises(RequestStopped, match='live_not_authorized'):
        runtime.authorize()


def test_live_authorization_requires_matching_hash_window_and_ledger(tmp_path):
    _, runtime_config, plan = config(authorized=True, qps=1,
        approvedConfigHash='bad', windowStart=datetime.now(timezone.utc)-timedelta(minutes=1),
        windowEnd=datetime.now(timezone.utc)+timedelta(minutes=1))
    runtime = PoiRuntime(runtime_config, plan, live=True)
    with pytest.raises(RequestStopped, match='live_not_authorized'):
        runtime.authorize()
    runtime_config = runtime_config.model_copy(update={'approved_config_hash': plan['configHash']})
    runtime = PoiRuntime(runtime_config, plan, live=True)
    with pytest.raises(RequestStopped, match='ledger_required'):
        runtime.authorize()
    with PoiLedger(tmp_path / 'ledger', runtime_config, plan['configHash']) as ledger:
        runtime.ledger = ledger
        assert runtime.authorize() is None
        ledger.data['status'] = 'completed'
        with pytest.raises(RequestStopped, match='run_not_active'):
            runtime.authorize()


def test_ledger_is_one_shot_and_does_not_reset_existing_state(tmp_path):
    _, runtime_config, plan = config()
    root = tmp_path / 'ledger'
    with PoiLedger(root, runtime_config, plan['configHash']) as ledger:
        ledger.data['sent'] = 7
        ledger.save()
    with pytest.raises(RequestStopped, match='run_already_started'):
        with PoiLedger(root, runtime_config, plan['configHash']):
            pass
    assert json.loads((root / 'ledger.json').read_text(encoding='utf-8'))['sent'] == 7


def test_config_rejects_naive_or_partial_approval_window():
    with pytest.raises(ValueError):
        RuntimeConfig(runId='x', windowStart='2026-09-13T00:00:00')
    with pytest.raises(ValueError):
        RuntimeConfig(runId='x', categoryBudgets={'unknown': 1})
