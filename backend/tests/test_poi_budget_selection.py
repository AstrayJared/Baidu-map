from pathlib import Path

import pytest

from app.poi.models import CATEGORIES, CollectionConfig, PoiCollectRequest, RuntimeConfig
from app.poi.planner import build_plan


def test_three_category_cli_example_is_valid_without_expanding_budget():
    config = CollectionConfig.model_validate_json(
        (Path(__file__).parents[1] / 'tools/poi-example.json').read_text(encoding='utf-8'))
    assert len(config.runtime.category_budgets) == 3
    assert build_plan(config.request, config.runtime)['requestUpperBound'] == 300


def test_default_runtime_has_every_budget():
    assert set(RuntimeConfig(runId='default').category_budgets) == set(CATEGORIES)


def test_requested_category_missing_budget_is_rejected_at_both_entrypoints():
    request = PoiCollectRequest(coordinateSystem='bd09ll', categories=['market', 'pharmacy'])
    runtime = RuntimeConfig(runId='missing', categoryBudgets={'market': 1})
    with pytest.raises(ValueError, match='every requested category'):
        build_plan(request, runtime)
    with pytest.raises(ValueError, match='every requested category'):
        CollectionConfig(request=request, runtime=runtime)


@pytest.mark.parametrize('budgets', [{}, {'unknown': 1}, {'market': True},
                                   {'market': 0}, {'market': 10001}, {'market': '1'}])
def test_invalid_budgets_are_rejected(budgets):
    with pytest.raises(ValueError):
        RuntimeConfig(runId='invalid', categoryBudgets=budgets)
