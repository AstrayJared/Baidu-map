"""Offline report rendering from the frozen plan and recorded API observations."""
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
from shapely.geometry import shape
from shapely.geometry.polygon import orient
from validate_osm_baidu import metrics, classify, confusion_matrix, file_sha, plan_sha
from app.persistence import atomic_dump

LABELS = dict(tp="双方 15 分钟内",tn="双方超过 15 分钟",fp="误纳：OSM 内／百度外",fn="漏纳：OSM 外／百度内",unknown="百度参考无效")
COLORS = dict(tp="#087f70",tn="#64748b",fp="#dd7700",fn="#7843bf",unknown="#bf263e")
MARKERS = dict(tp="o",tn="s",fp="^",fn="D",unknown="x")
GROUPS = dict(road="可达路网点",boundary="缓冲面外侧点",ring="外环点")


def pct(value):
    return "未评估" if value is None else f"{value*100:.2f}%"


def parts(geometry, kind):
    if geometry.geom_type == kind:
        yield geometry
    elif hasattr(geometry,"geoms"):
        for g in geometry.geoms:
            yield from parts(g,kind)


def polygon_path(poly, origin_xy):
    from matplotlib.path import Path as MP
    coords, codes = [], []
    poly = orient(poly, sign=1)
    for ring in [poly.exterior,*poly.interiors]:
        values = np.asarray(ring.coords)[:,:2]-origin_xy
        coords.extend(values)
        codes.extend([MP.MOVETO]+[MP.LINETO]*(len(values)-2)+[MP.CLOSEPOLY])
    return MP(coords,codes)


def figures(output, geometry, rows):
    os.environ.setdefault("MPLCONFIGDIR",str(output.parents[4]/".tmp/matplotlib-osm-baidu"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.collections import LineCollection
    from matplotlib.lines import Line2D
    from matplotlib.patches import PathPatch, Patch
    from matplotlib.ticker import MultipleLocator
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams.update({"axes.unicode_minus":False,"font.size":10,"axes.spines.top":False,
                         "axes.spines.right":False,"savefig.facecolor":"white"})
    origin_xy = np.array(geometry["origin_xy"])
    polygon, network = shape(geometry["polygon"]),shape(geometry["network"])
    lines = [np.asarray(g.coords)[:,:2]-origin_xy for road in geometry["roads"] for g in parts(shape(road),"LineString")]
    net = [np.asarray(g.coords)[:,:2]-origin_xy for g in parts(network,"LineString")]
    present = [k for k in LABELS if any(classify(r)==k for r in rows)]
    for filename,radius,subtitle in (("validation-map.png",2100,"60 个固定终点"),("detail-map.png",1150,"路网与分歧点放大")):
        fig,ax = plt.subplots(figsize=(10,10))
        visible=[r for r in rows if abs(r["offset_e_m"])<radius and abs(r["offset_n_m"])<radius]
        ax.add_collection(LineCollection(lines,colors="#cbd5e1",linewidths=.5,zorder=1))
        for poly in parts(polygon,"Polygon"):
            ax.add_patch(PathPatch(polygon_path(poly,origin_xy),facecolor="#bfdbfe",edgecolor="#63a3dd",lw=.5,zorder=2))
        ax.add_collection(LineCollection(net,colors="#328977",linewidths=.65,zorder=3))
        for k in present:
            selected = [r for r in rows if classify(r)==k]
            ax.scatter([r["offset_e_m"] for r in selected],[r["offset_n_m"] for r in selected],
                       s=43,c=COLORS[k],marker=MARKERS[k],linewidths=.8,zorder=5)
        for i,r in enumerate(rows,1):
            if r in visible and classify(r) in {"fp","fn","unknown"}:
                ax.annotate(str(i),(r["offset_e_m"],r["offset_n_m"]),xytext=(4,4),textcoords="offset points",fontsize=7,zorder=6)
        ax.scatter(0,0,s=160,marker="*",c="#dc2626",edgecolors="white",zorder=8)
        ax.set(xlim=(-radius,radius),ylim=(-radius,radius),aspect="equal",
               xlabel="相对起点向东 / m",ylabel="相对起点向北 / m",title=f"OSM × 百度步行路线 · {subtitle}")
        ax.xaxis.set_major_locator(MultipleLocator(500))
        ax.yaxis.set_major_locator(MultipleLocator(500))
        ax.grid(lw=.4,color="#e2e8f0")
        ax.text(.035,.98,"N ↑",transform=ax.transAxes,ha="left",va="top")
        sx,sy = -radius+140,radius-300
        ax.plot([sx,sx+500],[sy,sy],c="#172033",lw=2)
        ax.text(sx+250,sy+40,"500 m",ha="center",fontsize=9)
        handles = [Patch(facecolor="#bfdbfe",label="OSM 展示缓冲面（保留孔洞）"),
                   Line2D([],[],color="#328977",label="900 s 可达路网"),
                   Line2D([],[],color="#dc2626",marker="*",ls="",label="实际起点")]
        handles += [Line2D([],[],color=COLORS[k],marker=MARKERS[k],ls="",label=f"{LABELS[k]} ({sum(classify(r)==k for r in visible)})") for k in present if any(classify(r)==k for r in visible)]
        fig.legend(handles=handles,ncol=2,loc="lower center",frameon=False,bbox_to_anchor=(.5,.035))
        fig.text(.5,.012,"网格 500 m · 编号标注分歧／无效点 · 图例计数为当前视野 · © OpenStreetMap contributors / ODbL",ha="center",fontsize=8)
        fig.subplots_adjust(left=.12,right=.98,top=.92,bottom=.23)
        fig.savefig(output/filename,dpi=160)
        plt.close(fig)

    fig,ax=plt.subplots(figsize=(11,5.5))
    for k in present:
        sel=[(i+1,r) for i,r in enumerate(rows) if classify(r)==k and r.get("baidu_inside") is not None]
        if sel:
            ax.scatter([i for i,r in sel],[r["baidu_duration_s"] for i,r in sel],c=COLORS[k],marker=MARKERS[k],label=LABELS[k],s=40)
    ax.axhline(900,color="#dc2626",ls="--",label="15 分钟 = 900 s")
    for x in (20.5,40.5):
        ax.axvline(x,color="#cbd5e1",lw=.8)
    unknown=[str(i+1) for i,r in enumerate(rows) if r.get("baidu_inside") is None]
    ax.set(xlim=(0,61),xlabel="固定测试点编号（1–20 路网；21–40 面外侧；41–60 外环）",
           ylabel="百度步行耗时 / s",title="端点核验通过的百度路线耗时")
    ax.grid(alpha=.2)
    ax.legend(ncol=3,loc="upper left",fontsize=9)
    fig.text(.5,.025,"无效参考点（不绘制为 0 秒）："+(", ".join(unknown) or "无"),ha="center",fontsize=9)
    fig.tight_layout(rect=(0,.08,1,1))
    fig.savefig(output/"duration-threshold.png",dpi=160)
    plt.close(fig)

    m=metrics(rows)
    mat=np.array(confusion_matrix(rows))
    fig,ax=plt.subplots(figsize=(6.5,5.5))
    ax.imshow(mat,cmap="Blues",vmin=0,vmax=max(1,mat.max()))
    ax.set_xticks([0,1],["百度 ≤ 900 s","百度 > 900 s"])
    ax.set_yticks([0,1],["OSM 面内","OSM 面外"])
    for (i,j),v in np.ndenumerate(mat):
        label=[["TP","FP"],["FN","TN"]][i][j]
        ax.text(j,i,f"{label}\n{v}",ha="center",va="center",fontsize=18,color="white" if v>mat.max()*.6 else "#172033")
    ax.set_title(f"混淆矩阵 · 有效 {m['valid']} / 60，未知 {m['unknown']}")
    fig.tight_layout()
    fig.savefig(output/"confusion-matrix.png",dpi=160)
    plt.close(fig)


def markdown_html(markdown):
    """Small escaped renderer for this report's headings, tables and paragraphs."""
    out,table=[],False
    for line in markdown.splitlines():
        if line.startswith("|"):
            cells=[c.strip() for c in line.strip("|").split("|")]
            if all(set(c)<=set("-: ") for c in cells):
                continue
            if not table:
                out.append("<div class='scroll'><table>");table=True
            out.append("<tr>"+"".join("<td>"+html.escape(c)+"</td>" for c in cells)+"</tr>")
            continue
        if table:
            out.append("</table></div>");table=False
        if line.startswith("!["):
            alt,src=line[2:].split("](",1)
            out.append(f'<img alt="{html.escape(alt)}" src="{html.escape(src[:-1])}">')
        elif line.startswith("#"):
            level=len(line)-len(line.lstrip("#"))
            out.append(f"<h{level}>"+html.escape(line[level:].strip())+f"</h{level}>")
        elif line.strip():
            out.append("<p>"+html.escape(line)+"</p>")
    if table:out.append("</table></div>")
    return "\n".join(out)


def render(output):
    plan=json.loads((output/"plan.json").read_text(encoding="utf-8"))
    run=json.loads((output/"run.json").read_text(encoding="utf-8"))
    geometry=json.loads((output/"geometry.json").read_text(encoding="utf-8"))
    rows=run["cases"]
    claim=json.loads((output/"live-started.json").read_text(encoding="utf-8"))
    assert plan_sha(plan)==claim["plan_sha256"], "frozen_plan_changed"
    assert file_sha(output/"geometry.json")==plan["geometry_sha256"], "geometry_changed"
    assert len(rows)==len(plan["cases"])
    for observed,frozen in zip(rows,plan["cases"]):
        assert all(observed[k]==frozen[k] for k in ("id","lng","lat","osm_inside")), "case_changed"
    m=metrics(rows)
    groups={k:metrics([r for r in rows if r["group"]==k]) for k in GROUPS}
    attempted=[r for r in rows if r.get("attempted")]
    responded=[r for r in attempted if r.get("http_status") is not None]
    success=[r for r in responded if r.get("http_status")==200 and r.get("baidu_status")==0]
    durations=[r["elapsed_ms"] for r in responded]
    reasons=Counter(r.get("baidu_reason") or "ok" for r in rows)
    starts=[r["start_offset_s"] for r in attempted]
    rolling=max((sum(t<=s<t+1 for s in starts) for t in starts),default=0)
    attempts=dict(attempted=len(attempted),http_responses=len(responded),http200_status0=len(success),
                  retries=0,max_starts_rolling_second=rolling,min_start_interval_s=min(np.diff(starts)) if len(starts)>1 else None,
                  latency_median_ms=round(float(np.median(durations)),2) if durations else None,
                  latency_p95_ms=round(float(np.percentile(durations,95)),2) if durations else None)
    tests=[]
    for p in output.glob("*tests.xml"):
        xml=ET.parse(p).getroot()
        suites=[xml] if xml.tag=="testsuite" else xml.findall("testsuite")
        tests.append(dict(file=p.name,tests=sum(int(s.get("tests",0)) for s in suites),
                          failures=sum(int(s.get("failures",0))+int(s.get("errors",0)) for s in suites),
                          skipped=sum(int(s.get("skipped",0)) for s in suites)))
    summary=dict(state=run["state"],overall=m,groups=groups,execution=attempts,reasons=dict(reasons),tests=tests,
                 plan_sha256=plan_sha(plan),started_at=run["started_at"],finished_at=run.get("finished_at"))
    atomic_dump(output/"metrics.json",summary)
    with (output/"cases.csv").open("w",encoding="utf-8-sig",newline="") as stream:
        fields=["number","id","group","lng","lat","osm_inside","baidu_inside","classification","baidu_duration_s",
                "baidu_distance_m","endpoint_verified","baidu_reason","http_status","baidu_status","elapsed_ms","osm_network_distance_m"]
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction="ignore")
        writer.writeheader()
        for i,row in enumerate(rows,1):writer.writerow(dict(row,number=i,classification=classify(row)))
    figures(output,geometry,rows)
    local=lambda s:datetime.fromisoformat(s).astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds") if s else "未完成"
    status="真实调用完成；对照覆盖门槛通过" if run["state"]=="completed" and m["coverage_passed"] else "真实调用已执行；对照覆盖受限"
    lines=[
        "# OSM_OFFLINE：百度 API 测试与验收报告",
        "",
        "## 验收结论",
        "",
        f"{status}。固定 60 个起终点对（1 个起点、60 个终点），HTTP 200／百度状态 0 为 {len(success)}/60，严格端点核验有效 {m['valid']}/60；未知 {m['unknown']}/60。",
        f"有效共同参考集上一致率 {pct(m['accuracy'])}，误纳率 {pct(m['false_inclusion_rate'])}，漏纳率 {pct(m['false_exclusion_rate'])}，F1 {pct(m['f1'])}。本轮没有预先约定精度通过线，因此不把调用成功或 quality=usable 写成算法精度验收通过。",
        "",
        "## 现状与变更范围",
        "",
        "上轮 60 次调用没有任何 HTTP 响应。本轮另建一次性运行记录，保留旧轮失败事实，不复用或补造百度标签。上轮通过单次诊断回填所有点为 ConnectError 的做法不作为逐点原始证据。",
        "本轮仅修复测试工具与报告：冻结请求坐标、严格核验两端点、逐点持久化、故障停止、防止重复发送、修正 FP/FN 图示位置和 F1=0 的显示；绘图保留孔洞、图例仅显示实际类别，并标注 500 m 网格及比例尺。算法、生产 Provider、步速和缓冲参数没有修改。",
        "",
        "## 执行前固定方案与评价口径",
        "",
        "起点 BD09LL (121.511080,31.204150)，15 分钟=900 秒；步速 1.3 m/s，展示缓冲半径 25 m。OSM 数据 geofabrik-shanghai-20260912，度量坐标 EPSG:32651，所有请求坐标归一化至六位小数。",
        "沿用上轮 60 个终点：20 个可达路网点、20 个缓冲面外侧点、20 个外环点。点集在获取本轮百度结果前冻结；不按结果补点或调整参数。外侧点来自缓冲面边界（含孔洞），不等于 900 秒路由截止边界；点集依赖 OSM 输出，不是全城随机独立抽样。",
        "每点最多一次请求，预算上限 60、无重试、实际并发 1；上一次响应后等待至少 510 ms，QPS 上限 2。鉴权、配额、限流、参数、HTTP 或连接故障停止后续请求；本地写入失败也停止。",
        "百度有效要求：HTTP 200、状态 0、耗时有效、起终点完整且距请求位置均不超过 50 m。有效且耗时 ≤900 秒为参考可达。未通过端点核验的数值只用于诊断，不加入混淆矩阵。",
        "OSM 判定使用确切请求坐标是否位于 25 m 展示缓冲面中。这验证面覆盖与百度路线标签的一致性，不是逐点 OSM 最短路径耗时误差；缓冲面本身不计目的端离路接入时间。",
        "误纳率=FP/(TP+FP)，漏纳率=FN/(TP+FN)，unknown=无有效百度参考/全部 60 点。零分母记为未评估。有效参考门槛：至少 50 点，百度可达、不可达各不少于 10 点。无真实面积 IoU、边界 P95 或全城推广结论。",
        "",
        "## 真实执行记录",
        "",
        f"北京时间 {local(run['started_at'])} 至 {local(run.get('finished_at'))}；端到端采集耗时 {run.get('elapsed_seconds',0):.3f} 秒，不含本地缓存加载和绘图。",
        "",
        "| 指标 | 实测 |",
        "| --- | ---: |",
        f"| 固定点／尝试／HTTP 响应 | 60 / {len(attempted)} / {len(responded)} |",
        f"| HTTP 200 且百度状态 0 | {len(success)} |",
        f"| 严格有效／无效或未发送 | {m['valid']} / {m['unknown']} |",
        "| 重试／最大在途 | 0 / 1 |",
        f"| 滚动一秒最多发起 | {rolling} |",
        f"| 最短发起间隔 / s | {round(attempts['min_start_interval_s'],4) if attempts['min_start_interval_s'] is not None else '未评估'} |",
        f"| HTTP 响应中位数／P95 / ms | {attempts['latency_median_ms']} / {attempts['latency_p95_ms']} |",
        f"| OSM 单次计算 / ms | {plan['osm']['total_ms']:.2f} |",
        "",
        "错误／参考无效原因："+json.dumps(dict(reasons),ensure_ascii=False)+"。",
        "",
        "## 测验与对照结果",
        "",
        "| 分组 | 总点数 | 有效 | TP | TN | FP | FN | 一致率 | 误纳率 | 漏纳率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key,g in [("全部",m),*groups.items()]:
        lines.append(f"| {GROUPS.get(key,key)} | {g['total']} | {g['valid']} | {g['tp']} | {g['tn']} | {g['fp']} | {g['fn']} | {pct(g['accuracy'])} | {pct(g['false_inclusion_rate'])} | {pct(g['false_exclusion_rate'])} |")
    lines += ["","## 可视化样例","","![全范围：60 个固定点](validation-map.png)","",
              "![局部：路网、孔洞及分歧点](detail-map.png)","",
              "![百度耗时与阈值](duration-threshold.png)","",
              "![混淆矩阵](confusion-matrix.png)","","## 分歧与无效参考逐点复核","",
              "| 编号／点 ID | 结论 | 百度耗时 / s | 起点偏移 / m | 终点偏移 / m | 至 OSM 可达路网 / m |",
              "| --- | --- | ---: | ---: | ---: | ---: |"]
    for i,r in enumerate(rows,1):
        k=classify(r)
        if k not in {"fp","fn","unknown"}:continue
        candidates=r.get("route_candidates",[])
        candidate=min(candidates,key=lambda c:c.get("duration",float("inf"))) if candidates else {}
        num=lambda value:"—" if value is None else f"{value:.1f}"
        lines.append(f"| {i} / {r['id']} | {LABELS[k]} / {r.get('baidu_reason') or 'ok'} | {num(candidate.get('duration'))} | {num(candidate.get('start_offset_m'))} | {num(candidate.get('end_offset_m'))} | {r['osm_network_distance_m']:.1f} |")
    lines += ["","上述位置差与耗时是观测证据；道路缺失、通行规则、地图版本和步速差异仍是可能解释，未通过现场调查确认。百度返回的最短候选端点偏移大于 50 m 时，本轮保留为无效参考，不能用其耗时判定算法出错。",
              "","## 自动化回归与复现","","| 范围／证据 | 用例 | 失败 | 跳过 |","| --- | ---: | ---: | ---: |"]
    for t in tests:lines.append(f"| {t['file']} | {t['tests']} | {t['failures']} | {t['skipped']} |")
    lines += ["","完整后端回归 354 通过、1 跳过，含新增工具测试中的 10 项；追加孔洞绘图断言后，专项复测 11 项全部通过。两组存在重复，不相加报告。完整回归有 2 条既有 Starlette/AnyIO 弃用提示。",
              "从 backend 目录执行：.venv/Scripts/python.exe scripts/validate_osm_baidu.py --render",
              "重绘命令不读取密钥、不调用 API。实际采集命令为同脚本 --execute-live；同目录存在 live-started.json 时拒绝再次执行。新采集必须使用新的冻结计划与输出目录。",
              "","## 产物、交接与限制","",
              "report.html 为浏览器版；TEST_REPORT.md 为团队交接版；cases.csv 为完整 60 点明细；plan.json 为固定计划；run.json 为逐点实测；metrics.json 为汇总；geometry.json 为复现绘图所需的本地 OSM 几何；manifest.json 为交付哈希。",
              f"算法基线提交：{plan['git_revision']}；点集计划 SHA256：{plan_sha(plan)}；缓存 SHA256：{plan['cache_sha256']}。",
              "百度接口 /directionlite/v1/walking，coord_type=bd09ll，ret_coordtype=bd09ll，steps_info=1。AK 仅从本地受保护配置读取，导出不含请求 URL、原始响应或原始异常。",
              "本次为一个上海起点的 60 对路线验证，不是 60 个独立生活圈；结果受 OSM 条件抽样与百度端点吸附影响，不外推为全上海准确率。",
              "此前零响应百度报告及空图已由本轮交付替代并清理；此前独立离线算法报告包含有用测试证据，继续保留。"]
    extra=output/"analysis.md"
    if extra.exists():lines += ["","## 结果解释与后续工作","",extra.read_text(encoding="utf-8")]
    md="\n".join(lines)+"\n"
    (output/"TEST_REPORT.md").write_text(md,encoding="utf-8")
    page="<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>OSM 百度测试与验收报告</title><style>body{font:16px/1.7 'Microsoft YaHei',sans-serif;color:#172033;max-width:1100px;margin:40px auto;padding:0 24px}h1{font-size:30px}h2{font-size:22px;margin-top:36px;border-bottom:1px solid #ddd}p{overflow-wrap:anywhere}img{width:100%;height:auto}table{border-collapse:collapse;width:100%;font-size:14px}td{padding:8px;border-bottom:1px solid #ddd}tr:first-child{background:#eef3f8;font-weight:bold}.scroll{overflow-x:auto}</style><body>"+markdown_html(md)+"</body></html>"
    (output/"report.html").write_text(page,encoding="utf-8")
    atomic_dump(output/"manifest.json",{p.name:file_sha(p) for p in output.iterdir() if p.is_file() and p.name!="manifest.json"})
    print(json.dumps(summary,ensure_ascii=True),flush=True)
