"""Boundary-focused report from frozen evidence, without new provider calls."""
from datetime import datetime
from pathlib import Path
import re
from statistics import median

from shapely.geometry import Point, shape

from tools.validate_hybrid import read, summarize


def report(run, destination, checks):
    measured = summarize(run)
    plan = read(run / 'validation-plan.json')
    task_info = read(run / 'generation-task.json')
    task = run / 'tasks' / task_info['task_id']
    diagnostics = read(task / 'diagnostics.json')['diagnostics']
    ledger = read(task / 'ledger.json')
    validation = read(run / 'validation-ledger.json')
    result = read(run / 'frozen-result.json')['isochrone']
    checked = read(checks)
    if not all(c['returncode'] == 0 for c in checked):
        raise ValueError('required_checks_not_passed')
    pct = lambda v: '无有效分母' if v is None else f'{v:.2%}'
    outcome = {'passed': '本地点抽样通过', 'failed': '未通过边界精度验收',
               'insufficient_evidence': '独立证据不足，不能判定通过'}[measured['outcome']]
    names = dict(boundary='边界（含轮廓直接测时）', inferred_fill='内部推断', interior='其余内部', exterior='远处圈外')
    rows = []
    for key, values in measured['strata'].items():
        rows.append(f"| {names[key]} | {values['planned']} | {values['selected']} | {values['decidable']} | {pct(values['accuracy'])} | {values['fp']} / {values['fn']} | {values['within_tolerance']} |")
    failures = {
        'insufficient_valid_reference_points': '总量、分层或轮廓组有效证据不足80%',
        'boundary_tolerance_accuracy_below_95pct': '边界容差一致率低于95%',
        'contour_duration_accuracy_below_95pct': '轮廓直接测时达标率低于95%',
        'unconfirmed_boundary_above_5pct': '未确认边段累计超过外轮廓长度5%',
        'extent_truncated': '圈面触及计算范围，存在截断',
    }
    boundary = measured['boundary_summary']
    contour = measured['contour']
    deviations = sorted(abs(r['evidence']['duration']-900) for r in measured['cases']
                        if r.get('kind') == 'contour' and r['classification'] != 'api_unknown'
                        and r['evidence'].get('duration') is not None)
    deviation_text = (f"中位数 {median(deviations):.1f} 秒、最大 {max(deviations):.1f} 秒"
                      if deviations else '无有效轮廓测时')
    sent = sorted(e['sent_at'] for e in ledger['events'] + validation['events'] if 'sent_at' in e)
    j, peak = 0, 0
    for i, stamp in enumerate(sent):
        while j < i and stamp-sent[j] >= 1:
            j += 1
        peak = max(peak, i-j+1)
    g = shape(diagnostics['metric_geometry'])
    near_count = sum(g.boundary.distance(Point(s['xy'])) <= 50 for s in ledger['samples'])
    invalid = sum(s['evidence']['reachable'] is None for s in ledger['samples'])
    far = sum(not g.covers(Point(s['xy'])) and g.boundary.distance(Point(s['xy'])) > 50 for s in ledger['samples'])
    band_valid = measured['strata']['boundary']['decidable'] - contour['decidable']
    comparison = '历史数据不可用，未制作比较。'
    old_run = run.parent / 'verification-v15'
    if (old_run / 'metrics.json').is_file():
        old = read(old_run / 'metrics.json')
        old_task = old_run / 'tasks' / read(old_run / 'generation-task.json')['task_id']
        old_d = read(old_task / 'diagnostics.json')['diagnostics']
        old_g = shape(old_d['metric_geometry'])
        old_ledger = read(old_task / 'ledger.json')
        old_near = sum(old_g.boundary.distance(Point(s['xy'])) <= 50 for s in old_ledger['samples'])
        old_far = sum(not old_g.covers(Point(s['xy'])) and old_g.boundary.distance(Point(s['xy'])) > 50
                      for s in old_ledger['samples'])
        # Historical comparison is computed in memory, never rewrites v1.5.
        old_boundary = [r for r in old['cases'] if r['group'] == 'boundary' and r['classification'] in ('tp', 'tn', 'fp', 'fn')]
        old_errors = sum((r['prediction'] and r['evidence']['duration'] > 915)
                         or (not r['prediction'] and r['evidence']['duration'] < 885) for r in old_boundary)
        comparison = f'''| 指标 | v1.5历史运行 | 本轮 |
| --- | ---: | ---: |
| 生成调用 | {old['generation_requests']} | {measured['generation_requests']} |
| 生成点中距最终边界≤50米的点 | {old_near} | {near_count} |
| 生成点中距最终圈面超过50米的圈外点 | {old_far} | {far} |
| 独立验证：边界／内部推断／其余内部／远处圈外 | 25／25／25／25 | {'／'.join(str(plan['allocations'][k]) for k in names)} |
| 边界两侧容差误纳＋漏纳 | {old_errors}/{len(old_boundary)}（事后按±15秒重算） | {measured['strata']['boundary']['fp']+measured['strata']['boundary']['fn']}/{band_valid}（另有轮廓测时失败） |
| 未确认边段比例 | 旧版未测量 | {pct(boundary['unconfirmed_fraction'])} |

新旧采样分布不同，尤其新版增加轮廓直接测时，不能将两版汇总率当作同分布的精度提升证明。'''
    timing = result['timing_seconds']
    check_names = dict(backend='后端回归', **{'life-circle-algorithm': '算法包回归'},
                       frontend_tests='前端测试', frontend_build='类型检查与构建',
                       boundary_validation_regression='边界专项回归（已包含在后端数量内）')
    test_lines = []
    for check in checked:
        matches = re.findall(r'(\d+) passed', check['output'])
        count = f"{matches[-1]}项" if matches else ''
        skipped = re.findall(r'(\d+) skipped', check['output'])
        suffix = f"，{skipped[-1]}项跳过" if skipped else ''
        test_lines.append(f"- {check_names.get(check['check'], check['check'])}：{count}通过{suffix}。")
    test_lines = '\n'.join(test_lines)
    text = f'''# 15分钟步行圈：边界优化与独立核验

{datetime.fromtimestamp(read(run / 'live-started.json')['started_at']).strftime('%Y-%m-%d')} · {result['algorithm_version']} · 原起点 BD09LL `{tuple(plan['origin'])}`

**结论：{outcome}。** 边界容差一致率 **{pct(measured['strata']['boundary']['accuracy'])}**；轮廓直接测时达标率 **{pct(contour['accuracy'])}**；未确认边段比例 **{pct(boundary['unconfirmed_fraction'])}**。

![15分钟圈及边界验证](assets/osm-baidu-check-v16.png)

## 边界与调用

- 生成 **{measured['generation_requests']} / 400** 次，独立验证 **{measured['validation_requests']} / 100** 次；合计 **{measured['total_new_requests']} / 500** 次。预算不互借，无自动重试或追加调用。
- 生成阶段：初始定位 {diagnostics['initial_requests']} 次、补充探测 {diagnostics['supplementary_requests']} 次、边界工作 {diagnostics['boundary_requests']} 次；任意半开滑动一秒窗口最多 {peak} 次。
- 边界工作次数按调用目的记账，不等于全部点都在最终边界50米内；本轮后者为 {near_count} 点。
- 成圈目标900秒，报告容差±15秒。内部推断与可达面同色；边界缺口须获得新增证据，不能用填充掩盖。
- 未确认时间边界 {boundary['unconfirmed_length_m']:.1f} / {boundary['length_m']:.1f} 米。局部目标25米，米制夹逼不能换算成全边界±15秒保证。
- 2.4×2.4公里只作计算上限；`extent_truncated={str(result['extent_truncated']).lower()}`，停止原因 `{result['stop_reason']}`，质量 `{result['quality']}`。

## 冻结后的独立验证

先冻结结果、配置、账本，再使用种子 `{plan['seed']}` 选点。与生成请求去重，实际归一化坐标重检分层。默认75／15／5／5；空分层转给边界，本轮不适用分层为 `{plan['unavailable_strata']}`；选点缺额为 `{plan['selection_shortfalls']}`。

| 分层 | 计划 | 选中 | 有效 | 容差一致率 | 误纳／漏纳 | 容差内 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

- 轮廓直接测时：有效 {contour['decidable']} / 计划 {contour['planned']}，超出容差 {contour['contour_miss']} 个；绝对耗时偏差{deviation_text}。
- 圈内 >915秒才记误纳，圈外 <885秒才记漏纳，885—915秒单列容差内。轮廓点直接要求885—915秒，超出单列“轮廓耗时超出容差”，不重复计入误纳漏纳。
- 严格900秒原始分类：`{measured['strict_counts']}`。有效性由原始百度证据决定；无效、未返回不因容差变成有效。
- 验收要求：总量、各适用分层及轮廓组均至少80%有效；边界容差一致率及轮廓直接测时达标率均至少95%；未确认边段≤5%；未截断。
- 未满足条件：{'；'.join(failures[k] for k in measured['failure_reasons']) or '无'}。

圈外分类评估的是最终圈面的空间纳入决定；局部证据支撑状态另存，不能据此宣称所有圈外区域均已探测不可达。未确认比例衡量已有局部交界证据覆盖，独立验证才检查其时间误差。本结果仅描述本地点的分层及配对样本，不代表面积准确率，也不保证全轮廓处处误差≤15秒。

## 与历史运行比较

{comparison}

## 本轮未达标的具体证据

- 生成阶段 {invalid} / {ledger['requests_used']} 个样本未取得有效可达标签；`{diagnostics['sampling_reasons']}` 为调用用途统计。
- 待补充证据的缺口仍有 {len(diagnostics['unresolved_gap_candidates'])} 处，未支撑外缘长 {diagnostics['unsupported_frontier_length_m']:.1f} 米。未知缺口优先队列消耗了后续细化额度，普通已知正负交界的进一步细化不足；这些位置没有被填充成已确认可达。
- 轮廓直接测时有 {contour['contour_miss']} 点超出±15秒，最大偏差 {max(deviations) if deviations else 0:.1f} 秒。因此减少圈外采样已做到，精度目标尚未做到，当前结果不宜当作精确的设施纳入边界。
- 下一步应先离线研究无效端点密集区的停止／换点规则及边段间公平分配，避免少数未知缺口耗尽预算。此次保留冻结失败结果，未根据独立验证标签重画圈面或追加请求。

## 检查与性能

{test_lines}

服务冷启动 {task_info['cold_load_seconds']:.1f} 秒；任务准备 {timing['preparation']:.1f} 秒；成面重建累计 {timing['geometry_rebuilds']:.1f} 秒；计算 {timing['compute_total']:.1f} 秒；任务合计 {timing['task_total']:.1f} 秒。接口完成创建、轮询及结果读取，设施检索仍未接入。

## 追溯

- 本地运行目录：`backend/.hybrid-ledgers/{run.name}/`；任务 `{task_info['task_id']}`。
- 冻结结果哈希：`{plan['result_hash']}`。
- 生成账本哈希：`{plan['generation_ledger_hash']}`。
- 原始点位、严格分类、容差分类及逐点秒数保存在 `validation-plan.json`、`validation-ledger.json` 和 `metrics.json`。验证标签未回流成圈；历史运行未改写。
'''
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding='utf-8')
