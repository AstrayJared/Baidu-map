"""Render frozen local evidence. No network calls, recomputation or relabelling."""
import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.path import Path as PlotPath
from matplotlib.patches import PathPatch, Patch
from matplotlib.lines import Line2D
from shapely.geometry import shape, mapping
from shapely.geometry.polygon import orient

from tools.validate_hybrid import read, summarize


def render(output, destination, *, measured_override=None):
    measured = summarize(output) if measured_override is None else measured_override
    tolerant = measured.get('schema_version') in ('hybrid-validation-v2', 'hybrid-validation-v3')
    task = output / "tasks" / read(output / "generation-task.json")["task_id"]
    data = read(task / "diagnostics.json")
    diagnostics = data["diagnostics"]
    origin = diagnostics["origin_xy"]
    ledger = read(task / "ledger.json")
    plt.rcParams.update({"font.family": "Microsoft YaHei", "axes.unicode_minus": False})
    fig, axes = plt.subplots(1, 2, figsize=(14, 7), constrained_layout=True)

    def polygon(ax, raw, color, alpha=1, hatch=None):
        geometry = shape(raw)
        parts = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
        for p in parts:
            if p.is_empty or p.geom_type != "Polygon":
                continue
            p = orient(p)
            vertices, codes = [], []
            for ring in [p.exterior, *p.interiors]:
                coords = [(x-origin[0], y-origin[1]) for x, y in ring.coords]
                vertices.extend(coords)
                codes.extend([PlotPath.MOVETO] + [PlotPath.LINETO]*(len(coords)-2) + [PlotPath.CLOSEPOLY])
            ax.add_patch(PathPatch(PlotPath(vertices, codes), facecolor=color, alpha=alpha,
                                  edgecolor="#245960", linewidth=.6, hatch=hatch))

    for ax in axes:
        polygon(ax, mapping(shape(diagnostics["metric_hard_obstacles"]).intersection(shape(diagnostics["metric_coverage_shell"]))), "#b9dff1")
        polygon(ax, diagnostics["metric_geometry"], "#82c9bb", .6)
        ax.scatter([0], [0], marker="*", s=150, color="#202d42", zorder=5)
        bounds = shape(diagnostics['metric_geometry']).bounds
        half = max(bounds[2]-bounds[0], bounds[3]-bounds[1]) / 2 + 100
        cx, cy = (bounds[0]+bounds[2])/2-origin[0], (bounds[1]+bounds[3])/2-origin[1]
        ax.set(xlim=(cx-half, cx+half), ylim=(cy-half, cy+half), aspect='equal',
               xlabel='距起点向东（米）', ylabel='距起点向北（米）')
        ax.grid(alpha=.18)

    for sample in ledger["samples"]:
        x, y = sample["xy"]
        reachable = sample["evidence"]["reachable"]
        color = "#1d7865" if reachable is True else "#b94747" if reachable is False else "#7b8390"
        axes[0].scatter([x-origin[0]], [y-origin[1]], c=color, s=9, alpha=.75)
    colours = {"tp":"#1d7865", "tn":"#4766a8", "fp":"#d62a2a", "fn":"#dd801d",
               "api_unknown":"#85858c", "algorithm_unknown":"#805ab1", "within_tolerance":"#6b9b91", "contour_miss":"#bd2774"}
    for row in measured["cases"]:
        x, y = row["xy"]
        axes[1].scatter([x-origin[0]], [y-origin[1]], c=colours[row["classification"]], s=24,
                        marker="x" if row["classification"].endswith("unknown") else "o")
    labelled = [r for r in measured["cases"] if r["classification"] in ("fp", "fn", "contour_miss")]
    spaced_labels = []
    for row in sorted(labelled, key=lambda r: -abs(r['evidence']['duration']-900)):
        if all(((row['xy'][0]-other['xy'][0])**2 + (row['xy'][1]-other['xy'][1])**2)**.5 >= 110 for other in spaced_labels):
            spaced_labels.append(row)
        if len(spaced_labels) == 12:
            break
    labelled = spaced_labels
    for index, row in enumerate(labelled):
        x, y = row["xy"]
        axes[1].annotate(f'{row["evidence"]["duration"]:.0f}s', (x-origin[0], y-origin[1]),
                         xytext=(12 if x >= origin[0] else -38, 16 if index % 2 == 0 else -20),
                         textcoords="offset points", fontsize=8,
                         arrowprops=dict(arrowstyle="-", color="#64748b", linewidth=.6),
                         bbox=dict(facecolor="white", edgecolor="none", alpha=.85, pad=1))
    axes[0].set_title(f'生成圈面：{ledger["requests_used"]} 次请求；颜色为算法内百度标签')
    axes[1].set_title(f'独立验证：{measured["validation_requests"]} 次；标注部分未达标点的秒数')
    handles = [Patch(color="#82c9bb", label="15分钟可达区域")]
    if diagnostics.get('metric_unconfirmed_boundary'):
        uncertain = shape(diagnostics['metric_unconfirmed_boundary'])
        parts = list(uncertain.geoms) if hasattr(uncertain, 'geoms') else [uncertain]
        for line in parts:
            if line.geom_type in ('LineString', 'LinearRing') and not line.is_empty:
                xs, ys = line.xy
                axes[0].plot([x-origin[0] for x in xs], [y-origin[1] for y in ys], color='#b76816', lw=1.2)
        if not uncertain.is_empty:
            handles.append(Line2D([], [], color='#b76816', lw=1.2, label='待确认边段'))
    axes[0].legend(handles=handles, loc="upper right", fontsize=8)
    labels = {"tp":"圈内可达", "tn":"圈外不可达", "fp":"误纳（>915秒）", "fn":"漏纳（<885秒）",
              "api_unknown":"百度证据无效", "algorithm_unknown":"算法未知区",
              "within_tolerance":"±15秒容差内", "contour_miss":"轮廓耗时超出容差"}
    if not tolerant:
        labels.update(fp='误纳（>900秒）', fn='漏纳（≤900秒）')
    present = {r['classification'] for r in measured['cases']}
    axes[1].legend(handles=[Patch(color=c, label=labels[k]) for k,c in colours.items() if k in present], loc="upper right", fontsize=8)
    overall = measured["overall"]
    accuracy = "未测得" if overall["accuracy"] is None else f'{overall["accuracy"]:.1%}'
    outcomes = {"passed":"本地点抽样通过", "failed":"未通过", "insufficient_evidence":"证据不足，不能判通过"}
    boundary_accuracy = measured['strata']['boundary']['accuracy']
    boundary_label = '未测得' if boundary_accuracy is None else f'{boundary_accuracy:.1%}'
    basis = '900秒目标 · ±15秒报告容差' if tolerant else '900秒严格分类（历史运行）'
    if measured.get('historical_policy_recalculation'):
        basis = '恢复原v1.5圈面 · 已有样本按±15秒复算'
    fig.suptitle(f'15分钟步行圈 · {basis}\n{outcomes[measured["outcome"]]}；一致率 {accuracy}，边界组 {boundary_label}', fontsize=14)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, dpi=180)
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.input, args.output)
