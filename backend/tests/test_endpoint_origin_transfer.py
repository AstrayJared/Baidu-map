"""第二个道路吸附点的观测，只有能被证明时才可以当作"从参考起点出发"的证据。

对着 `tools/endpoint_origin_transfer.py` 的离线原型钉住三件事：

1. **判定的边界**：`d_B ± t` 整段落在阈值同一侧才给标签，压线的一侧是可达（求解器用
   `duration <= 900` 判可达），另一侧不能确定不可达。
2. **真实失败形状**（20 米分裂）在基线里合不拢环，转移后合得拢，且精度不降。
3. **反例**：两点隔着河（连接耗时 600 秒）时，有界的转移**一条都不接受**、仍然不发布几何；
   去掉那条界就会发布一个面积明显偏小、看起来却"正常"的区域。

全部离线：解析替身、无网络、无真实调用。
"""
import asyncio

import pytest

from tools.endpoint_origin_transfer import (SPEED_TRUTH_RADIUS, THRESHOLD_SECONDS, Endpointless,
                                            TransferProvider,
                                            TransferOrigin, classify_transfer, run_case,
                                            transfer_interval)
from tools.endpoint_radial_experiment import ORIGIN, Synthetic, radius


# -- 1. 判定本身 -------------------------------------------------------------------

def test_the_interval_is_the_measured_connection_on_both_sides():
    assert transfer_interval(700.0, 20.0) == (680.0, 720.0)


@pytest.mark.parametrize('duration,connection,expected', [
    (700.0, 20.0, 'reachable'),      # 720 < 900
    (880.0, 20.0, 'reachable'),      # 上界正好 900：仍然确定可达（求解器判 <= 900）
    (950.0, 20.0, 'unreachable'),    # 下界 930 > 900
    (920.0, 20.0, 'unknown'),        # 下界正好 900：d_A 可能就是 900，不可判不可达
    (890.0, 20.0, 'unknown'),
    (910.0, 20.0, 'unknown'),
    (900.0, 0.0, 'reachable'),       # 连接为零时退化成"就是同一起点"
])
def test_only_a_conclusive_interval_gets_a_label(duration, connection, expected):
    assert classify_transfer(duration, connection) == expected


def test_the_threshold_matches_the_solver():
    assert THRESHOLD_SECONDS == 900.0
    assert classify_transfer(THRESHOLD_SECONDS, 0.0) == 'reachable'


@pytest.mark.parametrize('duration,connection', [
    (float('nan'), 20.0), (700.0, float('nan')), (700.0, -1.0), (float('inf'), 20.0)])
def test_unusable_inputs_are_refused_rather_than_guessed(duration, connection):
    with pytest.raises(ValueError):
        classify_transfer(duration, connection)


# -- 2. 真实失败形状 ----------------------------------------------------------------

def test_the_second_snap_point_alone_leaves_the_ring_unclosed():
    """基线：7 条观测被起点一致性拒收，环合不拢、没有几何 —— 与两次真实体检同形。"""
    row = asyncio.run(run_case('split20', lambda: TransferOrigin(ORIGIN), SPEED_TRUTH_RADIUS,
                               transfer=False))
    assert row['geometry'] is False
    assert row['ringAssembled'] is False
    assert set(row['rejectedReasons']) == {'different_actual_origin'}
    assert row['maxGapDegrees'] >= 90.0


def test_a_measured_20m_connection_closes_the_ring_without_losing_accuracy():
    """转移后环合上了，而且与真值的 IoU 不比单起点基线差：0.9827 对 0.9823。"""
    baseline = asyncio.run(run_case('circle', lambda: Synthetic('circle'), radius('circle', 0.0),
                                    transfer=True))
    row = asyncio.run(run_case('split20', lambda: TransferOrigin(ORIGIN), SPEED_TRUTH_RADIUS,
                               transfer=True))
    assert row['geometry'] is True and row['ringAssembled'] is True
    assert row['maxGapDegrees'] < 90.0
    assert row['iouVsTruth'] > 0.95
    assert row['iouVsTruth'] >= baseline['iouVsTruth'] - 0.02
    # 代价是**一次**连接实测，之后按起点对复用；无法确定的那些照旧被拒收。
    assert row['transfers']['connectionCalls'] == 1
    assert row['transfers']['accepted'] > 0 and row['transfers']['refused'] > 0
    assert set(row['transfers']['verdicts']) <= {'reachable', 'unreachable', 'unknown'}
    # 转移不改变求解器自己的派发计数：它仍然一个不超过预算。
    assert row['calls'] <= 200


def test_a_transfer_changes_the_attribution_only_not_the_measurement():
    """被转移的观测里，实测的终点与时长的**原值**保持不动 —— 冲突判定因此仍然看得见真相。

    这一点很重要：求解器对"同一坐标出现两个不同时长"判 `conflicting_observations`，
    依据是位置与时长。转移只回答"这条时长是从哪个起点算的"，不该动这两样。
    """
    async def run():
        provider = TransferProvider(TransferOrigin(ORIGIN))
        first = await provider.query_walking_time(ORIGIN, ORIGIN, 0)
        assert first.route_origin == ORIGIN          # 第一条自己立下参考起点
        target = provider.inner.projection.to_geographic((-500.0, -300.0))  # 落在楔形内
        observation = await provider.query_walking_time(ORIGIN, target, 0)
        assert provider.accepted, '楔形内的观测应当被转移'
        assert tuple(observation.route_origin) == ORIGIN
        assert tuple(observation.route_destination) == tuple(target)
        assert observation.observed_duration is not None
    asyncio.run(run())


def test_a_cancelled_run_transfers_nothing_and_publishes_nothing():
    """取消先到：不转移、不发布，与没有这条改动时一样。"""
    from app.algorithms.baidu_e82 import compute_e82
    from app.engines.baidu_e82 import e82_request
    from life_circle.models import CancelToken

    async def run():
        token = CancelToken()
        token.cancel()
        provider = TransferProvider(TransferOrigin(ORIGIN))
        result = await compute_e82(e82_request(ORIGIN, 200), provider, token)
        assert result.geometry is None and result.quality == 'insufficient'
        assert provider.accepted == [] and provider.connection_calls == 0
    asyncio.run(run())


def test_when_both_origins_agree_no_transfer_is_ever_attempted():
    """单一起点时结果与基线逐项相同：这条改动在不需要它的场景里是惰性的。"""
    row = asyncio.run(run_case('circle', lambda: Synthetic('circle'), radius('circle', 0.0),
                               transfer=True))
    assert row['transfers']['accepted'] == 0
    assert row['transfers']['refused'] == 0
    assert row['transfers']['connectionCalls'] == 0
    assert row['geometry'] is True and row['ringAssembled'] is True


def test_missing_endpoints_have_nothing_to_transfer():
    row = asyncio.run(run_case('endpointless', lambda: Endpointless(ORIGIN), SPEED_TRUTH_RADIUS,
                               transfer=True))
    assert row['geometry'] is False
    assert row['transfers']['accepted'] == 0
    assert row['transfers']['connectionCalls'] == 0
    assert set(row['rejectedReasons']) == {'missing_endpoints'}


# -- 3. 反例：隔着河的时候，那条界就是全部 --------------------------------------------------

def test_a_river_between_the_snap_points_refuses_every_transfer():
    """20 米的直线距离，600 秒的绕行：一条都不确定，于是照旧不发布任何几何。"""
    row = asyncio.run(run_case('split20-barrier', lambda: TransferOrigin(ORIGIN, detour_seconds=600.0),
                               SPEED_TRUTH_RADIUS, transfer=True))
    assert row['geometry'] is False and row['ringAssembled'] is False
    assert row['transfers']['accepted'] == 0
    assert row['transfers']['refused'] > 0
    assert row['transfers']['verdicts'] == ['unknown']


def test_without_the_bound_the_same_river_is_published_wrong():
    """去掉界以后，同样一次运行会发布一个面积明显偏小、看起来却正常的区域。

    这是"必须设界"的证据：两条路都给出 `quality=partial` 与合上的环，
    只有对着真值量出来的 IoU 把它们分开（0.64 对 0.98）。
    """
    naive = asyncio.run(run_case('split20-barrier', lambda: TransferOrigin(ORIGIN, detour_seconds=600.0),
                                 SPEED_TRUTH_RADIUS, transfer=True, naive=True))
    bounded_split = asyncio.run(run_case('split20', lambda: TransferOrigin(ORIGIN),
                                         SPEED_TRUTH_RADIUS, transfer=True))
    assert naive['geometry'] is True and naive['ringAssembled'] is True
    assert naive['quality'] == bounded_split['quality']
    assert naive['iouVsTruth'] < 0.8 < bounded_split['iouVsTruth']
    assert naive['areaKm2'] < bounded_split['areaKm2']
