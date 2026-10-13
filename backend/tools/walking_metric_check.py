"""三角不等式在真实路网上的验证：|d_A − d_B| ≤ t_AB 到底成不成立、有多紧。

离线原型（`tools/endpoint_origin_transfer.py`）用这条不等式把第二个道路吸附点的观测
归到同一个参考起点。它是**假设**，所以必须拿真实路网量一遍，而不是只在合成替身上成立就算数。

这里用本机那份 OSM 上海步行路网（`data/osm/`，与生产同一个包）：

* 在每个片区随机取若干对点 A、B，**直线相距约 20 米**（与 2026-10-10 那两次真实体检的
  20.19 米同量级），其中相当一部分会落在河两岸 —— 那正是"直线很近、步行很远"的形状；
* 再随机取若干目标点，量出 d_A、d_B 与 t_AB（A→B），逐一检查 `|d_A − d_B| ≤ t_AB`；
* 同时记录两件事：**松弛量** `t_AB − |d_A − d_B|`（界的松紧），以及 `A→B` 与 `B→A` 的差
  （步行网若真有单向边，这里会露出来）。

只读本机数据，无网络、无真实调用；结论只覆盖**这份 OSM 步行网**，不外推到百度的路网，
也不外推到百度返回的时长口径（见 evidence/10 §7 的限制说明）。

用法（cwd=backend）：
    .venv/bin/python -m tools.walking_metric_check --pairs 8 --destinations 5
    .venv/bin/python -m tools.walking_metric_check --json
"""
import argparse
import json
import math
import random
import statistics
import sys

from life_circle.coordinates import LocalProjection, normalize

from app.algorithms.osm_offline.engine import OsmOfflineEngine
from app.config import load_settings
from tools.diagnostic_common import no_network
from tools.graph_walking_benchmark import ORIGINS, SEARCH_TIME_S, Walker

#: 与真实失败样本同量级：两处吸附点相距约 20 米。
PAIR_DISTANCE_M = 20.0
#: 目标点取在 15 分钟圈附近：那里才是"标签会不会翻面"的地方。
DESTINATION_MIN_M = 300.0
DESTINATION_MAX_M = 1200.0
SEED = 20261011
#: 浮点噪声的容差：插值出来的 slack 常在 ±1e-12 秒量级，那不是违反。
VIOLATION_TOLERANCE_S = 1e-6
#: slack 小于这个值就算"界取等"：路径恰好穿过另一个吸附点。
TIGHT_TOLERANCE_S = 1e-6
#: 真实失败的那次体检中心（2026-10-10，31.299213,121.50847）：最该被量到的就是它。
EXTRA_AREAS = {'real_centre': (121.508470, 31.299213)}
AREAS = {**ORIGINS, **EXTRA_AREAS}


def jitter(anchor, rng, low, high):
    """以 anchor 为心的局部极坐标里取一点（米），返回经纬度。"""
    local = LocalProjection(normalize(anchor))
    angle = rng.uniform(0.0, 2 * math.pi)
    radius = rng.uniform(low, high)
    return local.to_geographic((radius * math.cos(angle), radius * math.sin(angle)))


def offset(point, rng, metres):
    local = LocalProjection(normalize(point))
    angle = rng.uniform(0.0, 2 * math.pi)
    return local.to_geographic((metres * math.cos(angle), metres * math.sin(angle)))


def walk_time(walker, point):
    """(秒, 吸附偏移米)；越界或不同连通分量返回 None。"""
    x, y = walker.store.projection.origin(normalize(point))
    seconds, _, offset_m = walker.at(x, y)
    if seconds is None:
        return None, offset_m
    return float(seconds), offset_m


def run_area(store, speed, name, anchor, *, pairs, destinations, rng):
    rows, skipped = [], 0
    for _ in range(pairs):
        a = jitter(anchor, rng, 0.0, 700.0)
        b = offset(a, rng, PAIR_DISTANCE_M)
        walker_a = Walker(store, a, graph_speed=speed)
        walker_b = Walker(store, b, graph_speed=speed)
        t_ab, offset_ab = walk_time(walker_a, b)
        t_ba, _ = walk_time(walker_b, a)
        for _ in range(destinations):
            target = jitter(a, rng, DESTINATION_MIN_M, DESTINATION_MAX_M)
            d_a, _ = walk_time(walker_a, target)
            d_b, _ = walk_time(walker_b, target)
            if None in (t_ab, t_ba, d_a, d_b) or max(t_ab, t_ba, d_a, d_b) >= SEARCH_TIME_S:
                # 连通分量不同或超出搜索半径：这一条量不出等式，如实跳过而不是当 0。
                skipped += 1
                continue
            # 换算成"步行时长"（与图速度无关：正数缩放不改变不等式）。
            scale = walker_a.graph_speed / 1.1
            row = {'area': name, 'tAB': t_ab * scale, 'tBA': t_ba * scale,
                   'dA': d_a * scale, 'dB': d_b * scale,
                   'straightM': offset_ab,
                   'difference': abs(d_a - d_b) * scale,
                   'slack': (t_ab - abs(d_a - d_b)) * scale}
            row['violation'] = row['slack'] < -VIOLATION_TOLERANCE_S
            row['tight'] = abs(row['slack']) <= TIGHT_TOLERANCE_S
            rows.append(row)
    return rows, skipped


def collect(store, speed, *, pairs, destinations):
    rng = random.Random(SEED)
    rows, skipped = [], 0
    for name, anchor in AREAS.items():
        area_rows, area_skipped = run_area(store, speed, name, anchor, pairs=pairs,
                                           destinations=destinations, rng=rng)
        rows += area_rows
        skipped += area_skipped
        print(f'  {name:16s} 三件套 {len(area_rows):4d} 条，跳过 {area_skipped:3d}',
              file=sys.stderr)
    return rows, skipped


def summarise(rows, skipped):
    def quantile(values, fraction):
        values = sorted(values)
        if not values:
            return None
        return values[min(len(values) - 1, int(fraction * len(values)))]

    slacks = [row['slack'] for row in rows]
    violations = [row for row in rows if row['violation']]
    tight = [row for row in rows if row.get('tight')]
    detours = [row for row in rows if row['tAB'] > 120.0]
    return {
        'triples': len(rows),
        'skipped': skipped,
        'violations': len(violations),
        'worstViolationSeconds': None if not violations else round(min(slacks), 6),
        'toleranceSeconds': VIOLATION_TOLERANCE_S,
        'tightShare': None if not rows else round(len(tight) / len(rows), 4),
        'slackSeconds': {
            'min': None if not slacks else round(min(slacks), 6),
            'p05': None if not slacks else round(quantile(slacks, .05), 3),
            'median': None if not slacks else round(statistics.median(slacks), 3),
            'p95': None if not slacks else round(quantile(slacks, .95), 3),
            'max': None if not slacks else round(max(slacks), 3),
        },
        'asymmetry': {
            'pairs': len(rows),
            'maxAbsSeconds': None if not rows else round(max(abs(r['tAB'] - r['tBA']) for r in rows), 3),
            'overOneSecond': sum(1 for r in rows if abs(r['tAB'] - r['tBA']) > 1.0),
        },
        'detourPairs': {
            'count': len(detours),
            'definition': 'tAB > 120 秒（直线 20 米却要走两分钟以上：多半隔着河或墙）',
            'violations': sum(1 for r in detours if r['violation']),
            'maxAbsDifferenceSeconds': None if not detours
            else round(max(r['difference'] for r in detours), 3),
            'maxSlackSeconds': None if not detours else round(max(r['slack'] for r in detours), 3),
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--pairs', type=int, default=8, help='每个片区的 A/B 点数')
    parser.add_argument('--destinations', type=int, default=5, help='每对点的目标点数')
    parser.add_argument('--areas', default='', help='只跑这些片区（逗号分隔），默认全部')
    parser.add_argument('--json', action='store_true')
    options = parser.parse_args(argv)
    if options.areas:
        wanted = {name.strip() for name in options.areas.split(',') if name.strip()}
        unknown = wanted - set(AREAS)
        if unknown:
            raise SystemExit(f'未知片区：{sorted(unknown)}')
        for name in list(AREAS):
            if name not in wanted:
                AREAS.pop(name)

    settings = load_settings()
    with no_network():
        engine = OsmOfflineEngine.load(settings)
        if engine.store is None:
            raise SystemExit(f'OSM 路网不可用：{engine.unavailable_reason}')
        rows, skipped = collect(engine.store, settings.walk_speed_mps,
                                pairs=options.pairs, destinations=options.destinations)
    summary = summarise(rows, skipped)
    if options.json:
        print(json.dumps({'summary': summary, 'rows': rows}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
