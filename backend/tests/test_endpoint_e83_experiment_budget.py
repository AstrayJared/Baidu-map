import asyncio
import json

import pytest

from tools.diagnostic_common import no_network
from tools import endpoint_e83_guided_experiment as experiment


def test_400_pair_uses_400_for_both_arms_and_rejects_output_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, 'LOADS', ((20, 5),))
    output = tmp_path / 'paired'

    async def run():
        with no_network():
            await experiment.run(output, ['ellipse'], [400], ['discover'], baseline_budget=400)
            with pytest.raises(FileExistsError):
                await experiment.run(output, ['ellipse'], [400], ['discover'], baseline_budget=400)

    asyncio.run(run())
    rows = json.loads((output / 'metrics.json').read_text(encoding='utf-8'))
    assert {row['mode'] for row in rows} == {'independent', 'discover'}
    assert len(rows) == 2
    assert all(row['budget'] == 400 and row['cost']['circle_calls'] == 400 for row in rows)
    assert all(row['cost']['poi_calls'] == 20 and row['cost']['total_calls'] == 420 for row in rows)
    audit = json.loads((output / 'audit.json').read_text(encoding='utf-8'))
    assert audit['live_calls'] == 0 and audit['geometry_failed'] == 0
    assert audit['source_hashes_unchanged'] and audit['fixture_unchanged']
