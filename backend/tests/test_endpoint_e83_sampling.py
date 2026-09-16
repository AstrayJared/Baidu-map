import asyncio
from dataclasses import replace

from life_circle.models import CancelToken
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.endpoint_radial_experiment import Synthetic
from test_endpoint_radial_boundary import request


def test_coverage_first_brackets_every_direction_before_fine_refinement():
    async def run():
        r = await compute_multicross_boundary(replace(request(80), concurrency=30),
            Synthetic('circle'), CancelToken(), parallel_sampling=True,
            edge_batch_size=30, radial_step=100, coverage_first=True)
        assert len(r['directions']) == 16
        assert all(d.get('bracket') for d in r['directions'])
        assert r['calls'] <= 80
        evidence = r['observationEvidence']
        first_midpoint = next(i for i, e in enumerate(evidence) if e['kind'] == 'boundary_midpoint')
        # 300 -> 480 -> 768 metres: three observations per ray, not four.
        assert sum(e['kind'] == 'direction_search' for e in evidence[:first_midpoint]) == 48
    asyncio.run(run())


def test_paused_cell_reactivates_on_new_evidence_and_progress():
    from tools.endpoint_e83_sampling import DiverseEdgeQueue
    q = DiverseEdgeQueue(cell_m=75)
    q.observe('first', (10, 10), (10, 10))
    q.observe('repeat', (11, 11), (10, 10))
    q.observe('repeat2', (12, 12), (10, 10))
    assert q.paused((10, 10))
    q.observe('new', (13, 13), (20, 20))
    assert not q.paused((10, 10))
    q.observe('repeat3', (14, 14), (20, 20))
    q.observe('repeat4', (15, 15), (20, 20))
    assert q.paused((10, 10))
    q.progress((10, 10))
    assert not q.paused((10, 10))


def test_diverse_batch_does_not_fill_all_slots_from_one_cell():
    from tools.endpoint_e83_sampling import DiverseEdgeQueue
    def edge(x, width):
        return (width, {'xy': [x-width/2, 0]}, {'xy': [x+width/2, 0]})
    q = DiverseEdgeQueue(cell_m=75)
    edges = {'a': edge(10, 200), 'b': edge(11, 190), 'c': edge(200, 100)}
    assert q.select(edges, 30) == ['a', 'c']


def test_recovery_probe_is_bounded_and_never_labels_pause_as_convergence():
    from tools.endpoint_e83_sampling import DiverseEdgeQueue
    q = DiverseEdgeQueue(cell_m=75, recovery_limit=1)
    for i in range(3):
        q.observe(str(i), (10+i, 0), (10, 0))
    edge = {'a': (100, {'xy': [-40, 0]}, {'xy': [60, 0]})}
    assert q.select(edge, 30) == ['a']
    assert q.select(edge, 30) == []
    assert q.paused((10, 0))


def test_research_variants_obey_budget_cancel_and_determinism():
    async def run():
        results = []
        for _ in range(2):
            provider = Synthetic('snap80')
            r = await compute_multicross_boundary(replace(request(160), concurrency=30),
                provider, CancelToken(), radial_step=100, parallel_sampling=True,
                edge_batch_size=30, diverse_batches=True, coverage_first=True)
            assert r['calls'] == provider.calls <= 160
            assert not r['completion']['resolutionReached']
            results.append(r['geometry'])
        assert results[0] == results[1]
        token = CancelToken(); token.cancel(); provider = Synthetic('circle')
        r = await compute_multicross_boundary(request(300), provider, token,
            diverse_batches=True, coverage_first=True)
        assert provider.calls == 0 and r['geometry'] is None
    asyncio.run(run())


def test_coverage_first_preserves_complete_refinement_history_and_jump_evidence():
    async def run():
        result = await compute_multicross_boundary(request(300), Synthetic('jump'),
            CancelToken(), coverage_first=True)
        for row in result['directions']:
            bracket = row['bracket']
            assert bracket['iterations'] >= 3
            assert sum(s['accepted'] for s in bracket['history']) == bracket['iterations']
            assert bracket['suspected_jump']
    asyncio.run(run())


def test_stagnation_only_keeps_unstalled_candidates_in_original_order():
    from tools.endpoint_e83_sampling import DiverseEdgeQueue
    q = DiverseEdgeQueue(spread=False, pause=True)
    edges = {'a': (200, {'xy': [-90, 0]}, {'xy': [110, 0]}),
             'b': (190, {'xy': [-84, 0]}, {'xy': [106, 0]}),
             'c': (100, {'xy': [150, 0]}, {'xy': [250, 0]})}
    assert q.select(edges, 30) == ['a', 'b', 'c']
    for i in range(3):
        q.observe(str(i), (10+i, 0), (10, 0))
    assert q.select(edges, 30) == ['c']


def test_diversity_only_never_pauses_a_cell_from_repeat_history():
    from tools.endpoint_e83_sampling import DiverseEdgeQueue
    q = DiverseEdgeQueue(spread=True, pause=False, recovery_limit=0)
    for i in range(3):
        q.observe(str(i), (10+i, 0), (10, 0))
    assert not q.paused((10, 0))
    edge = {'a': (100, {'xy': [-40, 0]}, {'xy': [60, 0]})}
    assert q.select(edge, 30) == ['a']


def test_unknown_queue_policy_fails_before_sending_queries():
    import pytest
    async def run():
        p = Synthetic('circle')
        with pytest.raises(ValueError, match='queue'):
            await compute_multicross_boundary(request(300), p, CancelToken(), edge_queue_policy='typo')
        assert p.calls == 0
    asyncio.run(run())
