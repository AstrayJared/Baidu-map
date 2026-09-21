"""Report independent audit of the restored algorithm without changing metric meanings."""
from tools.validate_hybrid import read, summarize


def report(run, destination, checks):
    m, plan = summarize(run), read(run/'validation-plan.json')
    checked = read(checks)
    if not all(c['returncode'] == 0 for c in checked):
        raise ValueError('checks_not_passed')
    names = dict(boundary='边界两侧', inferred_fill='内部推断', interior='其余内部', exterior='远处圈外')
    percent = lambda v: '无有效分母' if v is None else f'{v:.2%}'
    rows = '\n'.join(f"| {names[k]} | {v['planned']} | {v['decidable']} | {percent(m['strict_strata'][k]['accuracy'])} | {percent(v['accuracy'])} | {v['fp']} / {v['fn']} |"
                     for k, v in m['strata'].items())
    outcome = dict(passed='本地点空间分类抽样通过', failed='未达到分类验收要求', insufficient_evidence='证据不足')[m['outcome']]
    text = f'''# 恢复版15分钟圈：独立验证

**{outcome}。** 使用原v1.5成圈算法；生成 {m['generation_requests']} 次，独立验证 {m['validation_requests']} 次。

验证点按冻结几何自动分配：`{plan['allocations']}`。远处圈外最多2点，其余优先边界；内部推断抽查随面积调整。圈面和账本先冻结，参考标签不回流成圈。

| 分层 | 计划 | 有效 | 严格900秒一致率 | ±15秒容差一致率 | 容差误纳／漏纳 |
| --- | ---: | ---: | ---: | ---: | ---: |
{rows}

885—915秒内的分类差异不记误纳／漏纳，原始严格分类保留。这里统计空间纳入，不把“轮廓点是否恰好900秒”混入分类一致率。无效证据继续弃判。

要求总有效数≥80、各适用分层至少80%有效、可达／不可达各至少20点；总体容差一致率≥90%，边界容差一致率≥95%。结论不代表全边界±15秒保证，也不提供旧算法未测量的“未确认长度比例”。

结果哈希：`{plan['result_hash']}`。所有工程检查通过，详细记录见检查文件；没有自动追加验证额度。
'''
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding='utf-8')
