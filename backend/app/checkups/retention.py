"""§5 B2 决策 2：到期之后剩下什么，以及怎么把明细删干净。

到期之后**保留**的是一份白名单汇总，而不是"原文件的删减版"：结论、分数区间、面积、计数、
停止原因与到期原因。没有设施名称、UID、地址、坐标，也没有任何几何 —— 白名单是**逐项列出来
的**，所以新加一个字段不会自动流进这里；反过来（黑名单）总有一天会漏掉一项，而漏掉的那一
项恰恰是坐标。

汇总在**定稿时**算好并随修订一起入库（``revisions.summary``）。这让到期变成一次纯删除：
不需要在删文件之前先读一遍那个文件 —— 那种"边读边删"的顺序一旦中断，剩下的就是既没有
明细、也没有汇总的记录。
"""
import json
import shutil

#: 汇总里允许出现的标量。不是标量的一律不带走 —— 几何、点位、名称都是"不是标量"的形状，
#: 而它们的共同点是：一旦漏进去就不是汇总了。
def _scalars(mapping) -> dict:
    if not isinstance(mapping, dict):
        return {}
    return {key: value for key, value in mapping.items()
            if isinstance(value, (str, int, float, bool)) or value is None}


def _counts(rows) -> dict:
    return {'total': len(rows)} if isinstance(rows, list) else {}


def _facility_summary(group) -> dict | None:
    if not isinstance(group, dict):
        return None
    counts = {key: len(value) for key, value in group.items()
              if isinstance(value, list) and key != 'queryCoverage'}
    out = _scalars(group)
    out.pop('queryDomain', None)          # 里面有原点坐标
    out.pop('queryIncompleteRegions', None)  # 未完成区域的坐标
    coverage = _scalars(group.get('queryAreaCoverage'))
    if coverage:
        # 只留数字与状态：分类名是目录里的类别，不是设施明细。
        out['queryAreaCoverage'] = {
            **coverage,
            **({'categories': list(group['queryAreaCoverage']['categories'])}
               if isinstance(group['queryAreaCoverage'].get('categories'), list) else {}),
        }
    out['counts'] = counts
    # 来源任务的页数：谁的数据构成了这一版。只留标识与计数，不留取数时刻之外的任何东西。
    sources = group.get('sourceTasks')
    if isinstance(sources, dict):
        out['sourceTasks'] = {name: _scalars(item) for name, item in sources.items()
                              if isinstance(item, dict)}
    return out


def _score_summary(scores) -> dict | None:
    if not isinstance(scores, dict):
        return None
    rows = [{'category': row.get('category'), **_scalars({
        key: row.get(key) for key in ('supported', 'coverageLowerPct', 'coverageUpperPct',
                                      'assessablePct', 'unknownPct', 'intervalWidthPct',
                                      'intervalDegenerate', 'unavailableReason', 'coveredM2',
                                      'gapM2', 'unknownM2', 'evidenceGrade')})}
            for row in scores.get('categories', []) if isinstance(row, dict)]
    overall = _scalars(scores.get('overall'))
    if isinstance(scores.get('overall'), dict):
        overall['missingCategories'] = list(scores['overall'].get('missingCategories') or [])
        overall['categories'] = list(scores['overall'].get('categories') or [])
    return {'ruleVersion': scores.get('ruleVersion'), 'domainAreaM2': scores.get('domainAreaM2'),
            'categories': rows, 'overall': overall or None}


def _verification_summary(evidence) -> dict | None:
    if not isinstance(evidence, dict):
        return None
    # 只留计数与状态：``facilities``／``conflicts``／``spotChecks`` 里是设施名、UID 与点位。
    return _scalars({key: evidence.get(key) for key in
                     ('status', 'provider', 'checked', 'failed', 'unresolved', 'reason')})


def _gaps_summary(gaps) -> dict | None:
    # 空字典与"没有这一栏"是同一件事：``_scalars`` 会把缺失的键填成 None，于是"没有"
    # 会变成一份全是 null 的汇总，读起来像"这一栏跑了但什么都没算出来"。
    if not isinstance(gaps, dict) or not gaps:
        return None
    # 灰区清单不带几何，只留面积与"这一栏有没有跑"。
    return _scalars({key: gaps.get(key) for key in
                     ('status', 'reason', 'compositeAreaM2', 'gapAreaM2', 'byCategoryM2',
                      'obstacleLayerAvailable', 'minLabelAreaM2', 'compositeMinCategories')})


def _accessibility_summary(evidence) -> dict | None:
    if not isinstance(evidence, dict):
        return None
    out = _scalars({key: evidence.get(key) for key in
                    ('status', 'gridStepM', 'refinedStepM', 'domainAreaM2', 'excludedAreaM2',
                     'views')})
    out['views'] = _scalars(evidence.get('views'))
    out['categories'] = [_scalars(row) for row in evidence.get('categories', [])
                         if isinstance(row, dict)]
    return out


def summary_of(snapshot: dict) -> dict:
    """一份修订的白名单汇总。**逐项列出**，未列出的字段不会出现在结果里。"""
    if not isinstance(snapshot, dict):
        return {}
    facilities = snapshot.get('facilities')
    verification = snapshot.get('verification')
    report = snapshot.get('report') if isinstance(snapshot.get('report'), dict) else {}
    water = snapshot.get('water') if isinstance(snapshot.get('water'), dict) else {}
    return {
        'schemaVersion': snapshot.get('schemaVersion'),
        'revision': snapshot.get('revision'),
        'stage': snapshot.get('stage'),
        'businessStatus': snapshot.get('businessStatus'),
        'generatedAt': snapshot.get('generatedAt'),
        'engine': _scalars(snapshot.get('engine')),
        'ruleVersions': _scalars((snapshot.get('trace') or {}).get('ruleVersions')),
        'facilitiesStatus': snapshot.get('facilitiesStatus'),
        'accessibilityStatus': snapshot.get('accessibilityStatus'),
        'facilities': _facility_summary(facilities),
        'accessibility': _accessibility_summary(snapshot.get('accessibility')),
        'scores': _score_summary(snapshot.get('scores')),
        'essentialServices': (None if not snapshot.get('essentialServices') else {
            'ruleVersion': snapshot['essentialServices'].get('ruleVersion'),
            'verificationStatus': snapshot['essentialServices'].get('verificationStatus'),
            'accessibility': _accessibility_summary(snapshot['essentialServices'].get('accessibility')),
            'gaps': _gaps_summary(snapshot['essentialServices'].get('serviceGaps'))}),
        'verification': _verification_summary(verification),
        'gaps': _gaps_summary((snapshot.get('service_gaps') or {})) or _gaps_summary(report.get('gaps')),
        'heatmap': _scalars({key: (snapshot.get('heatmap') or {}).get(key)
                             for key in ('metric', 'estimated', 'stepM')}),
        'water': _scalars({key: water.get(key) for key in
                           ('obstacleLayerAvailable', 'osmDataVersion', 'sourcePbfSha256',
                            'domainAreaM2', 'reviewedAreaM2', 'unreviewedAreaM2',
                            'conflictAreaM2')}),
        'warnings': [issue.get('code') for issue in snapshot.get('warnings', [])
                     if isinstance(issue, dict)],
        'limitations': [text for text in report.get('limitations', []) if isinstance(text, str)],
    }


def delete_details(root, task_id: str) -> list[str]:
    """删掉一个任务的受管明细目录，返回仍然存在的路径（正常情况下为空）。

    整个目录一起删，而不是按索引逐条删：目录里可能有**没有索引行**的修订文件
    （写文件成功、写索引之前进程就没了），照索引清理会让这些明细永远留在盘上。
    只删 ``root/tasks/<task_id>`` 这一个目录，绝不触碰额度账本、OSM 数据或任何别的目录。
    """
    directory = root / 'tasks' / task_id
    shutil.rmtree(directory, ignore_errors=True)
    return [str(directory)] if directory.exists() else []


def dump(summary: dict) -> str:
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)
