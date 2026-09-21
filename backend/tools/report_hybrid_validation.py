"""Write the review report from frozen ledgers and isolated test evidence."""
import argparse
import math
from pathlib import Path
import re

from shapely.geometry import Point, shape

from tools.validate_hybrid import read, summarize


def report(run, destination, checks):
    if read(run / 'validation-plan.json').get('schema_version') == 'hybrid-validation-v2':
        from tools.report_hybrid_v16 import report as boundary_report
        return boundary_report(run, destination, checks)
    if read(run / 'validation-plan.json').get('schema_version') == 'hybrid-validation-v3':
        from tools.report_hybrid_restored import report as restored_report
        return restored_report(run, destination, checks)
    measured = summarize(run)
    task_info = read(run / "generation-task.json")
    task = run / "tasks" / task_info["task_id"]
    ledger = read(task / "ledger.json")
    diagnostics = read(task / "diagnostics.json")["diagnostics"]
    result = read(run / "frozen-result.json")
    core = result["isochrone"]
    plan = read(run / "validation-plan.json")
    validation = read(run / "validation-ledger.json")
    overall = measured["overall"]
    percent = lambda value: "无有效分母" if value is None else f"{value:.2%}"
    interval = lambda value: "无有效分母" if value is None else f"{percent(value[0])}–{percent(value[1])}"
    outcomes = {"passed": "本地点分层抽样验收通过", "failed": "分类一致率未达到验收线",
                "insufficient_evidence": "独立精度证据不足，不能判定通过"}
    outcome = outcomes[measured["outcome"]]
    sample_status = {}
    for sample in ledger["samples"]:
        validity = sample["evidence"]["validity"]
        sample_status[validity] = sample_status.get(validity, 0) + 1
    origin = diagnostics["origin_xy"]
    half = core["config"]["analysis_half_width_m"]
    outside = sum(max(abs(s["xy"][0]-origin[0]), abs(s["xy"][1]-origin[1])) > half for s in ledger["samples"])
    sent = sorted(e["sent_at"] for e in ledger["events"] + validation["events"] if "sent_at" in e)
    # Half-open rolling one-second windows, same accounting as the rate gate.
    j, peak = 0, 0
    for i, timestamp in enumerate(sent):
        while j < i and timestamp-sent[j] >= 1:
            j += 1
        peak = max(peak, i-j+1)
    tests = read(checks)
    if not all(c["returncode"] == 0 for c in tests):
        raise ValueError("isolated_checks_not_passed")
    passed = {}
    for check in tests:
        matches = re.findall(r"(\d+) passed", check["output"])
        passed[check["check"]] = int(matches[-1]) if matches else None
    failures = []
    if overall["decidable"] < 80:
        failures.append(f"有效可判定点 {overall['decidable']} < 80")
    for name, group in measured["strata"].items():
        required = math.ceil(group["planned"]*.8)
        if group["decidable"] < required:
            failures.append(f"{name} 可判定 {group['decidable']} < {required}")
    if overall["tp"]+overall["fn"] < 20:
        failures.append(f"可判定样本中真实可达 {overall['tp']+overall['fn']} < 20")
    if overall["tn"]+overall["fp"] < 20:
        failures.append(f"可判定样本中真实不可达 {overall['tn']+overall['fp']} < 20")
    if overall["accuracy"] is not None and overall["accuracy"] < .9:
        failures.append(f"一致率 {percent(overall['accuracy'])} < 90%")
    rows = []
    names = dict(boundary="边界附近", inferred_fill="推断填充", interior="其余圈内", exterior="圈外")
    for group, values in measured["strata"].items():
        rows.append(f"| {names[group]} | {values['planned']} | {values['decidable']} | {values['api_unknown']} | {values['algorithm_unknown']} | {percent(values['accuracy'])} | {values['fp']} / {values['fn']} |")
    timing = core["timing_seconds"]
    geometry = shape(diagnostics["metric_geometry"])
    bounds = [geometry.bounds[0]-origin[0], geometry.bounds[1]-origin[1], geometry.bounds[2]-origin[0], geometry.bounds[3]-origin[1]]
    errors = [r for r in measured['cases'] if r['classification'] in ('fp', 'fn')]
    durations = lambda kind: '、'.join(f"{r['evidence']['duration']:.0f}" for r in errors if r['classification'] == kind) or '无'
    boundary_errors = sum(r['group'] == 'boundary' for r in errors)
    distances = [r['boundary_distance_m'] for r in errors]
    error_range = f'{min(distances):.2f}–{max(distances):.2f}' if distances else '未测得'
    exterior = measured['strata']['exterior']
    fill = measured['strata']['inferred_fill']
    text = f"""# OSM＋百度 Hybrid v1.5：范围、精度与交付核验

2026-09-16 · 900 秒步行圈 · 唯一起点 BD09LL `(121.51392519758, 31.313079085826)`

**结论：2.4 km × 2.4 km 范围和专用接口已实现，完整真实 API 流程已完成；{outcome}。**

本轮新增生成请求 **{ledger['requests_used']}** 次，独立核验 **{validation['requests_used']}** 次，合计 **{measured['total_new_requests']} / 500** 次。无自动重试、未补充其他地点、未在参考标签返回后调整算法重新验收。

生成阶段通过 FastAPI 的 ASGI 接口完成创建、轮询与结果读取，百度请求为真实网络调用；没有接入浏览器页面。首次验证守卫将圈面外未解析水线也视为阻塞。核查确认各项数据可用且该水线不影响最终圈面后，仅对原冻结结果启动尚未执行的 100 次独立验证。原圈面、400 条账本、警告与降级状态均未改写；`blocked.json` 保留为该阶段的历史记录。

![范围与独立精度核验](assets/osm-baidu-check.png)

## 范围与实际额度

- 实际米制计算域为起点东西、南北各 ±{half:.0f} 米，面积 {(2*half)**2:,.0f} m²。不是半径 2.4 km 或半径 1.2 km 的圆。
- 算法账本越界样本 **{outside}**；所有候选以及六位小数的实际请求坐标均先检查范围。越界候选不发送、不计费；四角参与二维支撑采样。
- 最终圈面面积 **{geometry.area:,.2f} m²**；相对起点边界 `[西, 南, 东, 北]` 为 `{[round(v, 2) for v in bounds]}` 米。
- `extent_truncated={str(core['extent_truncated']).lower()}`；质量为 `{core['quality']}`，停止原因为 `{core['stop_reason']}`，数据就绪模式为 `{core['readiness']['mode']}`。
- 原版 400 点中有 62 点在新范围外，且无有效可达点；旧圈面全部位于新范围。但新采样会重新分配额度，本轮生成相对旧版实际差额是 **{400-ledger['requests_used']} 次**，不能把 62 点直接说成节省 62 次。
- 本轮生成及验证发送时间合并检查，任意半开滑动一秒窗口最多 **{peak} 次**。请求上限仍为生成 400、验证 100；验证不挤占或借用生成预算。

生成标签统计：`{sample_status}`。原始耗时、偏移超限、失败与未知均留存，不将无效证据改写为可达或不可达。

## 独立精度验收

先冻结 API 结果、配置、算法账本，再以固定种子 `{plan['seed']}` 选点。四组计划各 25 点；空分层在请求前转入边界组，本轮不适用分层为 `{plan['unavailable_strata']}`。点坐标与算法请求去重，参考标签未参与成面。边界附近指最终边界两侧 50 米带内的陆地点。

| 分层 | 计划 | 有效可判定 | 百度无效/未返回 | 算法未知 | 一致率 | 误纳 / 漏纳 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

- 可判定总数 **{overall['decidable']} / 100**，分类一致率 **{percent(overall['accuracy'])}**，Wilson 95% 区间 **{interval(overall['accuracy_wilson_95'])}**。
- 误纳率 **{percent(overall['false_inclusion_rate'])}**，区间 **{interval(overall['false_inclusion_wilson_95'])}**；分母为预测圈内且百度证据有效的点。
- 漏纳率 **{percent(overall['false_exclusion_rate'])}**，区间 **{interval(overall['false_exclusion_wilson_95'])}**；分母为可判定样本中的百度真实可达点。
- 百度证据未知 **{overall['api_unknown']}**，另有百度有效但算法处于未知域 **{overall['algorithm_unknown']}**；两者互斥记账，合计未知占比 **{percent(overall['unknown_fraction'])}**。
- 通过要求：至少 80 个有效可判定点、各适用分层至少 80% 可判定、真实可达和不可达各至少 20 个、总体一致率至少 90%。本轮判定：**{outcome}**。
- 未满足条件：{'；'.join(failures) if failures else '无'}。

**边界组需单独看待：一致率为 {percent(measured['strata']['boundary']['accuracy'])}，不能被内部和外围较高的一致率掩盖。** 本轮 {len(errors)} 个误判中 {boundary_errors} 个在边界组：{overall['fp']} 个误纳点百度耗时为 {durations('fp')} 秒，{overall['fn']} 个漏纳点为 {durations('fn')} 秒；它们距当前边界约 {error_range} 米。这是误判点与估计边界的距离，不是独立测出的全边界误差。

这些数字仅描述本地点的分层有效参考点，不能解读为圈面面积准确率或全边界误差保证。较高的一致率也不能抵消大量未知。未测量全边界米制误差；算法返回的 `boundary_error_estimate` 仅是已观察径向夹逼宽度。百度预计耗时是本次参考标准，不等于现场实走时间。

## 填充、水体与性能

- 推断填充面积 `{diagnostics['inferred_fill_area_m2']:,.2f} m²`；算法自身新增填充样本统计 `{diagnostics['fill_sample_stats']}`，不是独立参考集。
- 最终面与硬水体重叠 `{diagnostics['hard_obstacle_overlap_m2']:.6g} m²`，非障碍内部残留 `{diagnostics['non_obstacle_interior_residual_m2']:.6g} m²`。
- 有效保留桥通道 `{len(diagnostics['bridge_passages'])}` 条；影响圈面的未解析水线 `{diagnostics['unresolved_water_lines_affecting_shell']}`。没有实桥样本时，只能声明机制测试通过。
- 硬障碍只覆盖当前 OSM 水体及允许的桥例外，未证明门禁、围墙、铁路等所有现实限制已处理完毕。

| 耗时口径 | 秒 |
| --- | ---: |
| 服务启动路网冷加载（独立于任务） | {task_info['cold_load_seconds']:.3f} |
| 任务准备，含风险/水体处理 | {timing['preparation']:.3f} |
| 其中水体索引加载 | {timing['obstacle_load']:.3f} |
| 百度请求响应区间之和，不含 QPS 间隔 | {timing['requests']:.3f} |
| 累计成面重建 | {timing['geometry_rebuilds']:.3f} |
| 计算阶段，含限流等待与请求 | {timing['compute_total']:.3f} |
| API 任务总耗时，不含服务启动 | {timing['task_total']:.3f} |

图在进程启动时加载一次；风险与水体空间索引按路径、文件版本和投影缓存。首次冷启动较慢，本轮不能用离线重建速度替代真实响应时间；尚未实测第二个热任务。

## 接口、提交与后续工作

专用 Hybrid 类型、OpenAPI、合成响应样例和独立客户端已经提供。客户端不使用旧网格解析器，支持创建、轮询、读取结果、按任务或客户端请求 ID 取消。任务错误码、数据降级、范围截断、空几何与坐标约定已明确；完整诊断只保存在本地。

隔离目录按当前待提交文件集重建，未复制 `.env`、OSM 数据、账本或 Python 环境，复用了已安装依赖；已断言加载的是隔离源码而非原目录的可编辑安装。结果：后端 **{passed['backend']} 通过、1 跳过**，算法包 **{passed['life-circle-algorithm']} 通过**，前端 **{passed['frontend_tests']} 通过**，类型检查与构建成功。跳过的是需要显式启用的真实 PBF 重建测试。此次不是在全新系统上重新下载、安装所有依赖。

**工程上具备作为待审阅版本提交的条件；不能标为跨地区精度已验收的稳定产品。** 未执行 Git 提交或远程推送，已有历史删除未扩大。实际提交时需包括新增源码、测试、类型、说明和工具；密钥、原始路网与本地证据继续排除。

现有前端页面未切换；`facilitiesStatus=not_integrated`。本阶段没有实现医疗教育检索，也没有调用设施接口。后续采用固定圈面 → 分类候选检索 → 点面筛选 → 按需步行确证，复用当前 POI 分页、UID 去重和账本；扩大医院/诊所/学校等类别目录，将空间纳入、推断区域与真实路线确证分开。详见 [Hybrid 设计及设施方案](../../HYBRID_ISOCHRONE_DESIGN.md)。

后续优化按本轮证据排序：

1. 将边界组的误纳/漏纳作为下一版调度研究对象，优先验证正负证据交界与狭窄缺口，评估在相同 400 次上限内将这些位置细化至 25 米的收益；不把总体 {percent(overall['accuracy'])} 当作边界精度已足够。
2. 圈外组 {exterior['algorithm_unknown']} 点属于算法未知、{exterior['api_unknown']} 点百度证据无效，只有 {exterior['decidable']}/{exterior['planned']} 可判定。优先改进稀疏支撑与道路端点候选，保持未知语义；不能直接把外围未知改成不可达来满足验收。
3. 推断填充组可判定 {fill['decidable']} 点，其中 {fill['tp']} 点可达、{fill['fp']} 点误纳，仍是有限样本（误纳率的 95% 区间为 {interval(fill['false_inclusion_wilson_95'])}），保留推断标记；后续设施可采用按需入口路线确证。
4. 在新的冻结参考集上重新比较调用量、边界组指标和未知率，再决定是否收紧默认预算。当前不能声称已经节省调用或后续无需大改；本轮不再申请或消耗额外验证额度。

## 追溯

- 本地运行目录：`backend/.hybrid-ledgers/verification-v15/`，任务 `{task_info['task_id']}`。
- 结果哈希：`{plan['result_hash']}`。
- 算法账本哈希：`{plan['generation_ledger_hash']}`。
- `validation-plan.json` 保存冻结点位及分层，`validation-ledger.json` 保存独立请求，`metrics.json` 保存逐点分类。
- 旧报告与图片保留在 `verification-v14/`；当前报告和图片均为新版。

复算指标：`python -m tools.validate_hybrid --summarize --output .hybrid-ledgers/verification-v15`。复绘需安装可选 `requirements-report.txt`，使用 `tools.render_hybrid_validation`。两者均不会新增百度请求。
"""
    destination.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checks", type=Path, required=True)
    args = parser.parse_args()
    report(args.input, args.output, args.checks)
